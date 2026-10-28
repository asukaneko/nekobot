"""
角色运行时引擎

CharacterRuntime 是角色模拟的编排中心，负责：
- before_turn: 读取角色卡/状态/关系/记忆，分析信号，生成 ReactionPlan，编译提示词
- after_turn: 更新情绪/关系，写入事件，抽取记忆

不直接处理 HTTP / Socket / QQ，仅依赖统一请求对象和抽象存储接口。
"""

import logging

from nbot.character.models import (
    CharacterIdentity,
    CharacterMemory,
    CharacterProfile,
    CharacterState,
    CharacterTurnContext,
    ReactionPlan,
    RelationshipState,
)
from nbot.core.background_tasks import submit_background_task
from nbot.events import names as _E

_log = logging.getLogger(__name__)


class CharacterRuntime:
    """角色运行时引擎，编排角色模拟的完整生命周期"""

    def __init__(
        self,
        profile_repo=None,
        state_repo=None,
        relationship_repo=None,
        memory_service=None,
        signal_analyzer=None,
        planner=None,
        prompt_builder=None,
        state_machine=None,
        world_book_store=None,
        hook_runtime=None,
        history_store=None,
    ):
        self.profile_repo = profile_repo
        self.state_repo = state_repo
        self.relationship_repo = relationship_repo
        self.memory_service = memory_service
        self.signal_analyzer = signal_analyzer
        self.planner = planner
        self.prompt_builder = prompt_builder
        self.state_machine = state_machine
        self._world_book_store = world_book_store
        self._hook_runtime = hook_runtime
        self.history_store = history_store
        self._event_logger = None  # lazy init

    def _record_state_history(self, state, relationship, *, source="turn", reason="",
                              message_id=""):
        """记录状态快照（失败不影响对话主流程）。"""
        store = self.history_store
        if store is None or state is None:
            return
        try:
            store.record(
                getattr(state, "character_id", "") or "",
                getattr(state, "scope_id", "") or "",
                state,
                relationship,
                source=source,
                reason=reason,
                message_id=message_id,
            )
        except Exception as exc:  # pragma: no cover - 历史记录失败不阻断对话
            _log.debug("[CharacterRuntime] 状态快照写入失败: %s", exc)

    def _emit_hook(self, event_type: str, identity=None, payload=None, context=None, chat_request=None):
        """Emit a hook event if hook_runtime is available. Non-blocking helper."""
        if not self._hook_runtime:
            return
        try:
            from nbot.hooks.models import RuntimeEvent
            conversation_id = ""
            if chat_request is not None:
                conversation_id = getattr(chat_request, "conversation_id", "") or ""
            if not conversation_id:
                conversation_id = getattr(identity, "scope_id", "") if identity else ""
            event = RuntimeEvent(
                type=event_type,
                source="character_runtime",
                character_id=getattr(identity, "character_id", "") if identity else "",
                user_id=getattr(identity, "target_id", "") if identity else "",
                conversation_id=conversation_id,
                payload=payload or {},
            )
            from nbot.hooks.async_utils import run_hook_coro
            run_hook_coro(self._hook_runtime.emit_event(event, context=context))
        except Exception as e:
            _log.debug("[CharacterRuntime] hook emit failed: %s", e)

    def before_turn(self, chat_request, identity: CharacterIdentity, recent_messages: list = None) -> CharacterTurnContext:
        """每轮对话前的角色模拟编排

        Args:
            chat_request: 统一聊天请求
            identity: 角色身份标识

        Returns:
            CharacterTurnContext 包含本轮所有角色上下文
        """
        # 读取角色卡
        profile = self._get_profile(identity)

        # 读取或创建角色运行时状态
        state = self._get_or_create_state(identity, profile)
        real_time_context = self._build_real_time_context(state, chat_request)
        circadian_state = self._build_circadian_state(chat_request)
        self._ensure_current_activity(state)

        # 读取或创建关系状态
        relationship = self._get_or_create_relationship(identity, profile)

        self._emit_hook(_E.CHARACTER_TURN_BEFORE, identity, {
            "user_message": getattr(chat_request, "content", "")[:200],
        }, chat_request=chat_request)

        _log.debug(
            "[CharacterRuntime] before_turn: character=%s target=%s "
            "profile.initial_state=%s state.mood=%s relationship=%s",
            identity.character_id,
            identity.target_id,
            profile.initial_state,
            state.mood if state else None,
            {
                "affection": relationship.affection if relationship else None,
                "trust": relationship.trust if relationship else None,
                "familiarity": relationship.familiarity if relationship else None,
                "dependency": relationship.dependency if relationship else None,
                "security": relationship.security if relationship else None,
            },
        )

        # 检索相关记忆
        memories = self._search_memories(identity, chat_request)
        self._emit_hook(_E.CHARACTER_MEMORY_RECALLED, identity, {
            "memory_count": len(memories),
        }, chat_request=chat_request)
        self._emit_hook("character.after_memory_retrieve", identity, {
            "memory_count": len(memories),
        }, chat_request=chat_request)

        # 分析用户输入信号
        signals = self._analyze_signals(chat_request, state, relationship)

        # 生成反应计划
        plan = self._plan_reaction(
            profile, state, relationship, memories, signals, chat_request
        )
        self._emit_hook("character.after_reaction_plan", identity, {
            "intent": plan.intent if plan else "",
            "tone": plan.tone if plan else "",
        }, chat_request=chat_request)

        # 世界书关键词匹配（多源上下文召回）
        world_book_entries = self._match_world_books(
            identity,
            chat_request,
            state=state,
            recent_messages=recent_messages,
            relationship=relationship,
        )
        self._emit_hook("character.after_world_book_match", identity, {
            "entry_count": len(world_book_entries) if world_book_entries else 0,
        }, chat_request=chat_request)

        # 编译提示词
        prompt_text = self._build_prompt(
            profile, state, relationship, memories, plan,
            world_book_entries=world_book_entries,
            identity=identity,
            chat_request=chat_request,
            real_time_context=real_time_context,
            circadian_state=circadian_state,
        )

        self._emit_hook("character.before_turn.finished", identity, {
            "mood": state.mood if state else "",
            "affection": relationship.affection if relationship else 0,
            "trust": relationship.trust if relationship else 0,
        }, context={
            "mood": state.mood if state else "",
            "mood_intensity": state.mood_intensity if state else 0,
            "energy": state.energy if state else 0,
            "affection": relationship.affection if relationship else 0,
            "trust": relationship.trust if relationship else 0,
            "familiarity": relationship.familiarity if relationship else 0,
            "dependency": relationship.dependency if relationship else 0,
            "security": relationship.security if relationship else 0,
            "jealousy": relationship.jealousy if relationship else 0,
        }, chat_request=chat_request)

        return CharacterTurnContext(
            profile=profile,
            state=state,
            relationship=relationship,
            memories=memories,
            signals=signals,
            plan=plan,
            prompt_text=prompt_text,
            world_book_entries=world_book_entries,
        )

    def after_turn(self, chat_request, result, turn_context: CharacterTurnContext) -> None:
        """每轮对话后的状态更新

        Args:
            chat_request: 统一聊天请求
            result: PipelineResult
            turn_context: before_turn 返回的上下文
        """
        if not self.state_machine:
            _log.warning("[CharacterRuntime] after_turn skipped: state_machine is None")
            return

        identity = CharacterIdentity(
            character_id=turn_context.profile.id if turn_context.profile else "",
            target_id=getattr(chat_request, "user_id", "") or "",
            scope_id=turn_context.state.scope_id if turn_context.state else "",
        )
        self._emit_hook("character.after_turn.started", identity)

        old_state = self._refresh_latest_state(turn_context.state)
        old_relationship = self._refresh_latest_relationship(turn_context.relationship)

        # 应用状态变化
        new_state, new_relationship = self.state_machine.apply(
            old_state=old_state,
            old_relationship=old_relationship,
            signals=turn_context.signals,
            plan=turn_context.plan,
            user_message=getattr(chat_request, "content", ""),
            assistant_message=getattr(result, "final_content", ""),
        )

        _log.debug(
            "[CharacterRuntime] after_turn: old_rel=%s new_rel=%s state_repo=%s rel_repo=%s",
            {
                "affection": old_relationship.affection,
                "trust": old_relationship.trust,
                "familiarity": old_relationship.familiarity,
                "dependency": old_relationship.dependency,
                "security": old_relationship.security,
            },
            {
                "affection": new_relationship.affection,
                "trust": new_relationship.trust,
                "familiarity": new_relationship.familiarity,
                "dependency": new_relationship.dependency,
                "security": new_relationship.security,
            },
            self.state_repo is not None,
            self.relationship_repo is not None,
        )

        # 保存状态
        if self.state_repo and new_state:
            self.state_repo.save(new_state)

        if self.relationship_repo and new_relationship:
            self.relationship_repo.save(new_relationship)

        self._record_state_history(
            new_state,
            new_relationship,
            source="turn",
            reason="每轮状态机更新",
            message_id=str(getattr(result, "message_id", "") or ""),
        )

        self._schedule_auto_state_adjustment(
            chat_request=chat_request,
            result=result,
            turn_context=turn_context,
            base_state=new_state,
            base_relationship=new_relationship,
            identity=identity,
        )

        # Emit state/relationship change events
        if new_state and old_state:
            if new_state.mood != old_state.mood:
                self._emit_hook("state.changed", identity, {
                    "field": "mood", "old": old_state.mood, "new": new_state.mood,
                })
        if new_relationship and old_relationship:
            rel_changes = {}
            for field in ("affection", "trust", "familiarity", "dependency", "security", "jealousy"):
                old_val = getattr(old_relationship, field)
                new_val = getattr(new_relationship, field)
                if old_val != new_val:
                    rel_changes[field] = {"old": old_val, "new": new_val}
            if rel_changes:
                self._emit_hook("relationship.changed", identity, rel_changes)

        self._emit_hook("character.after_state_update", identity, {
            "mood": new_state.mood if new_state else "",
            "mood_intensity": new_state.mood_intensity if new_state else 0,
            "energy": new_state.energy if new_state else 0,
        }, context={
            "mood": new_state.mood if new_state else "",
            "mood_intensity": new_state.mood_intensity if new_state else 0,
            "energy": new_state.energy if new_state else 0,
            "affection": new_relationship.affection if new_relationship else 0,
            "trust": new_relationship.trust if new_relationship else 0,
            "familiarity": new_relationship.familiarity if new_relationship else 0,
            "dependency": new_relationship.dependency if new_relationship else 0,
            "security": new_relationship.security if new_relationship else 0,
            "jealousy": new_relationship.jealousy if new_relationship else 0,
        })

        # Web snapshot / timeline are written after after_turn from this context.
        # Keep them on the just-saved values instead of the before_turn baseline.
        if new_state:
            turn_context.state = new_state
        if new_relationship:
            turn_context.relationship = new_relationship

        # 记忆抽取（如果配置了记忆服务）
        if self.memory_service:
            try:
                self.memory_service.extract_and_save_if_needed(
                    chat_request=chat_request,
                    result=result,
                    turn_context=turn_context,
                )
                self._emit_hook(_E.CHARACTER_MEMORY_WRITTEN, identity)
                self._emit_hook("memory.after_extract", identity)
            except Exception as exc:
                _log.warning("[CharacterRuntime] 记忆抽取异常: %s", exc)

        # Review Pipeline（规则版）
        self._run_review(chat_request, result, turn_context, identity, new_relationship)

        self._emit_hook("character.after_turn.finished", identity, {
            "mood": new_state.mood if new_state else "",
            "affection": new_relationship.affection if new_relationship else 0,
        })
        self._emit_hook(_E.CHARACTER_TURN_AFTER, identity, {
            "mood": new_state.mood if new_state else "",
            "affection": new_relationship.affection if new_relationship else 0,
            "trust": new_relationship.trust if new_relationship else 0,
        })

    def _schedule_auto_state_adjustment(
        self,
        *,
        chat_request,
        result,
        turn_context: CharacterTurnContext,
        base_state: CharacterState,
        base_relationship: RelationshipState,
        identity: CharacterIdentity,
    ) -> None:
        """Run the slow AutoState evaluator after the reply has been sent."""
        if not base_state or not base_relationship or not turn_context:
            return

        metadata = dict(getattr(chat_request, "metadata", {}) or {})
        user_message = getattr(chat_request, "content", "") or ""
        assistant_message = getattr(result, "final_content", "") or ""
        conversation_id = getattr(chat_request, "conversation_id", "") or ""
        result_error = getattr(result, "error", None)
        profile = turn_context.profile

        def run_auto_state() -> None:
            try:
                from nbot.character.auto_state import update_state_from_recent_turns

                adjusted_state, adjusted_relationship, changed = update_state_from_recent_turns(
                    profile=profile,
                    state=base_state,
                    relationship=base_relationship,
                    user_message=user_message,
                    assistant_message=assistant_message,
                    metadata=metadata,
                    conversation_id=conversation_id,
                    result_error=result_error,
                )
                if not changed:
                    return
                self._save_async_auto_state_delta(
                    base_state=base_state,
                    base_relationship=base_relationship,
                    adjusted_state=adjusted_state,
                    adjusted_relationship=adjusted_relationship,
                    identity=identity,
                )
            except Exception as exc:
                _log.warning(
                    "[CharacterRuntime] auto state adjustment failed: %s",
                    exc,
                    exc_info=True,
                )

        submit_background_task(
            "auto_state_adjustment",
            run_auto_state,
            serial_key="auto_state",
        )

    def _save_async_auto_state_delta(
        self,
        *,
        base_state: CharacterState,
        base_relationship: RelationshipState,
        adjusted_state: CharacterState,
        adjusted_relationship: RelationshipState,
        identity: CharacterIdentity,
    ) -> None:
        """Save AutoState changes on top of the latest persisted values."""
        latest_state = self._refresh_latest_state(base_state) or base_state
        latest_relationship = (
            self._refresh_latest_relationship(base_relationship) or base_relationship
        )

        if self.state_repo and adjusted_state:
            if adjusted_state.mood != base_state.mood:
                latest_state.mood = adjusted_state.mood
            latest_state.mood_intensity = max(
                0.0,
                min(
                    1.0,
                    latest_state.mood_intensity
                    + (adjusted_state.mood_intensity - base_state.mood_intensity),
                ),
            )
            latest_state.energy = max(
                0,
                min(
                    100,
                    int(latest_state.energy + (adjusted_state.energy - base_state.energy)),
                ),
            )
            self.state_repo.save(latest_state)

        rel_changes = {}
        if self.relationship_repo and adjusted_relationship:
            for field in (
                "affection",
                "trust",
                "familiarity",
                "dependency",
                "security",
                "jealousy",
            ):
                delta = getattr(adjusted_relationship, field) - getattr(base_relationship, field)
                if not delta:
                    continue
                old_value = getattr(latest_relationship, field)
                new_value = max(0, min(100, int(old_value + delta)))
                setattr(latest_relationship, field, new_value)
                rel_changes[field] = {"old": old_value, "new": new_value}
            self.relationship_repo.save(latest_relationship)

        if adjusted_state and adjusted_state.mood != base_state.mood:
            self._emit_hook("state.changed", identity, {
                "field": "mood",
                "old": base_state.mood,
                "new": adjusted_state.mood,
            })
        if rel_changes:
            self._emit_hook("relationship.changed", identity, rel_changes)

        if adjusted_state or adjusted_relationship:
            self._record_state_history(
                latest_state,
                latest_relationship,
                source="auto_state",
                reason="AI 自动状态评估",
            )

        _log.debug(
            "[CharacterRuntime] async auto state adjustment applied: mood=%s intensity=%.2f",
            latest_state.mood if latest_state else "",
            latest_state.mood_intensity if latest_state else 0,
        )

    def _refresh_latest_state(self, state: CharacterState) -> CharacterState:
        if not self.state_repo or not state:
            return state
        try:
            latest = self.state_repo.get(state.character_id, state.scope_id)
            return latest or state
        except Exception:
            return state

    def _refresh_latest_relationship(self, relationship: RelationshipState) -> RelationshipState:
        if not self.relationship_repo or not relationship:
            return relationship
        try:
            latest = self.relationship_repo.get(
                relationship.character_id,
                relationship.target_id,
            )
            return latest or relationship
        except Exception:
            return relationship

    def _get_profile(self, identity: CharacterIdentity) -> CharacterProfile:
        """获取角色卡"""
        if self.profile_repo:
            profile = self.profile_repo.get(identity.character_id)
            if profile:
                return profile
        return CharacterProfile(name=identity.character_id)

    def _get_or_create_state(
        self, identity: CharacterIdentity, profile: CharacterProfile
    ) -> CharacterState:
        """获取或创建角色运行时状态"""
        if self.state_repo:
            state = self.state_repo.get_or_create(
                identity.character_id,
                identity.scope_id,
                initial_state=profile.initial_state,
            )
            if state:
                return state
        return CharacterState(
            character_id=identity.character_id,
            scope_id=identity.scope_id,
        )

    def _get_or_create_relationship(self, identity: CharacterIdentity, profile: CharacterProfile) -> RelationshipState:
        """获取或创建关系状态"""
        if self.relationship_repo:
            rel = self.relationship_repo.get_or_create(
                identity.character_id,
                identity.target_id,
                initial_state=profile.initial_state,
            )
            if rel:
                return rel
        return RelationshipState(
            character_id=identity.character_id,
            target_id=identity.target_id,
        )

    def _search_memories(
        self, identity: CharacterIdentity, chat_request
    ) -> list[CharacterMemory]:
        """检索相关记忆"""
        if not self.memory_service:
            return []
        try:
            return self.memory_service.search(
                character_id=identity.character_id,
                target_id=identity.target_id,
                query=getattr(chat_request, "content", ""),
                limit=8,
            )
        except Exception:
            return []

    def _analyze_signals(self, chat_request, state, relationship):
        """分析用户输入信号"""
        if not self.signal_analyzer:
            return None
        try:
            return self.signal_analyzer.analyze(
                getattr(chat_request, "content", ""),
                state=state,
                relationship=relationship,
            )
        except Exception:
            return None

    def _plan_reaction(
        self, profile, state, relationship, memories, signals, chat_request
    ) -> ReactionPlan:
        """生成反应计划"""
        if not self.planner:
            return ReactionPlan()
        try:
            return self.planner.plan(
                profile=profile,
                state=state,
                relationship=relationship,
                memories=memories,
                signals=signals,
                user_message=getattr(chat_request, "content", ""),
            )
        except Exception:
            return ReactionPlan()

    def _build_prompt(self, profile, state, relationship, memories, plan,
                      world_book_entries=None, identity=None, chat_request=None,
                      real_time_context=None, circadian_state=None) -> str:
        """编译提示词"""
        from nbot.character.prompt_builder import build_character_injections
        from nbot.character.prompt_stack import PromptStack

        stack = PromptStack()

        # 将状态/关系/记忆/计划注册到 PromptStack
        build_character_injections(
            stack,
            profile=profile,
            state=state,
            relationship=relationship,
            memories=memories,
            plan=plan,
        )

        # 注入世界书
        _log.debug("[WorldBook] _build_prompt: entries=%d", len(world_book_entries) if world_book_entries else 0)
        if world_book_entries:
            from nbot.character.world_book_injector import inject_world_book
            inject_world_book(stack, world_book_entries)

        self.inject_prompt_extras(
            stack,
            identity=identity,
            chat_request=chat_request,
            real_time_context=real_time_context,
            circadian_state=circadian_state,
        )

        # 合成最终提示词
        return stack.render()

    def inject_prompt_extras(
        self,
        stack,
        *,
        identity=None,
        chat_request=None,
        real_time_context=None,
        circadian_state=None,
    ) -> None:
        """Inject runtime-only prompt extras into an external PromptStack."""
        self._inject_real_time_context(stack, real_time_context)
        self._inject_circadian_state(stack, circadian_state)
        if identity and chat_request:
            # MemoryFS 三层必读注入（用户关系摘要 + 剧情摘要 + 日记）
            self._inject_memory_fs(stack, identity, chat_request)
            self._inject_self_correction(stack, identity, chat_request)
            self._inject_offline_plot_update(stack, identity, chat_request)

    def _build_real_time_context(self, state, chat_request) -> dict:
        try:
            from nbot.review.time_context import build_current_real_time_context

            previous_turn_time = getattr(state, "last_active_at", "") if state else ""
            context = build_current_real_time_context(previous_turn_time)
            metadata = getattr(chat_request, "metadata", None)
            if not isinstance(metadata, dict):
                metadata = {}
                chat_request.metadata = metadata
            metadata["real_time_context"] = context
            return context
        except Exception as exc:
            _log.warning("[CharacterRuntime] real time context failed: %s", exc, exc_info=True)
            return {}

    def _inject_real_time_context(self, stack, real_time_context) -> None:
        if not real_time_context:
            return
        try:
            from nbot.character.prompt_stack import PromptStack
            from nbot.review.time_context import format_real_time_prompt_context

            stack.add(
                key="real_time.continuity",
                content=format_real_time_prompt_context(real_time_context),
                priority=PromptStack.PRIORITY_CHARACTER_STATE + 1,
                scope="turn",
            )
        except Exception as exc:
            _log.debug("[CharacterRuntime] real time prompt injection failed: %s", exc)

    def _build_circadian_state(self, chat_request) -> dict:
        """构建昼夜节律状态并写入 chat_request.metadata 供下游使用。"""
        try:
            from nbot.review.time_context import build_circadian_state

            context = build_circadian_state()
            metadata = getattr(chat_request, "metadata", None)
            if not isinstance(metadata, dict):
                metadata = {}
                chat_request.metadata = metadata
            metadata["circadian_state"] = context
            return context
        except Exception as exc:
            _log.warning("[CharacterRuntime] circadian state build failed: %s", exc, exc_info=True)
            return {}

    def _inject_circadian_state(self, stack, circadian_state) -> None:
        if not circadian_state:
            return
        try:
            from nbot.character.prompt_stack import PromptStack
            from nbot.review.time_context import format_circadian_prompt

            stack.add(
                key="character.circadian",
                content=format_circadian_prompt(circadian_state),
                priority=PromptStack.PRIORITY_CHARACTER_STATE + 2,
                scope="turn",
            )
        except Exception as exc:
            _log.debug("[CharacterRuntime] circadian prompt injection failed: %s", exc)

    def _ensure_current_activity(self, state) -> None:
        """确保角色运行时状态有 current_activity，缺失时基于昼夜节律规则推断。

        这是保底机制：心跳未维护 scene.current_activity 时由规则填充。
        心跳维护的值优先级更高（由 _run_session_heartbeat_execution 写入）。
        同一小时内活动稳定，避免每轮对话活动都变。
        """
        try:
            if not state:
                return
            scene = state.scene if isinstance(state.scene, dict) else {}
            existing = str(scene.get("current_activity") or "").strip()
            if existing:
                # 已有心跳维护的活动，保留
                return
            from nbot.review.time_context import infer_current_activity

            activity = infer_current_activity()
            state.scene = scene
            state.scene["current_activity"] = activity
            state.scene["activity_source"] = "inferred"  # 标记来源，心跳写入时为 "heartbeat"
            # 注意：这里不 save，因为 before_turn 后续 _apply_state 会持久化
        except Exception as exc:
            _log.debug("[CharacterRuntime] ensure current_activity failed: %s", exc)

    def _match_world_books(
        self,
        identity,
        chat_request,
        state=None,
        recent_messages: list = None,
        relationship=None,
    ) -> list:
        """匹配世界书关键词（多源上下文召回）"""
        if not self._world_book_store:
            _log.debug("[WorldBook] store is None, skipping")
            return []
        try:
            from nbot.character.world_book_matcher import (
                WorldBookRecallContext,
                match_entries_v2,
            )
            world_books = self._world_book_store.list_all()
            user_message = getattr(chat_request, "content", "")

            _log.debug(
                "[WorldBook] matching: books=%d char_id=%s msg=%r recent_msgs=%d scene=%s",
                len(world_books), identity.character_id,
                user_message[:60] if user_message else "",
                len(recent_messages) if recent_messages else 0,
                bool(state.scene) if state else False,
            )

            # 构建召回上下文：场景 + 心情/精力 + 六维关系，供 state_triggers 使用
            scene = dict(state.scene or {}) if state and hasattr(state, "scene") else {}
            if state:
                scene.setdefault("mood", state.mood)
                scene.setdefault("mood_intensity", state.mood_intensity)
                scene.setdefault("energy", state.energy)
            if relationship is not None:
                for field_name in (
                    "affection", "trust", "familiarity",
                    "dependency", "security", "jealousy",
                ):
                    scene.setdefault(field_name, getattr(relationship, field_name, 0))

            recall_context = WorldBookRecallContext(
                latest_user_message=user_message,
                recent_messages=recent_messages or [],
                scene=scene,
                character_id=identity.character_id,
                target_id=getattr(identity, "target_id", ""),
                scope_id=getattr(identity, "scope_id", ""),
            )

            # V2 多源召回
            results = match_entries_v2(recall_context, world_books, identity.character_id)
            results = self._apply_world_book_injection_policy(identity, results)
            entries = [r.entry for r in results]

            _log.debug("[WorldBook] matched %d entries", len(entries))
            return entries
        except Exception as exc:
            _log.warning("[CharacterRuntime] world book match failed: %s", exc, exc_info=True)
            return []

    def _apply_world_book_injection_policy(self, identity, results: list) -> list:
        """执行世界书条目的冷却轮数与单会话注入次数限制。"""
        try:
            from nbot.character.world_book_state import get_world_book_state_store

            scope_key = "{}:{}".format(
                getattr(identity, "character_id", "") or "unknown",
                getattr(identity, "scope_id", "")
                or getattr(identity, "target_id", "")
                or "default",
            )
            store = get_world_book_state_store()
            turn = store.begin_turn(scope_key)
            if not results:
                return results
            return store.filter_and_record(scope_key, results, turn)
        except Exception as exc:
            _log.debug("[WorldBook] injection policy skipped: %s", exc)
            return results

    def _inject_memory_fs(self, stack, identity, chat_request) -> None:
        """将 MemoryFS 三层必读内容注入到 PromptStack（非阻塞）。

        注意：identity.target_id 可能是 scope_id 格式（如 web:{session_id}），
        与 auto_memory 保存时使用的 user_id 不一致，导致 user_persona/character_persona
        路径无法匹配。Pipeline 阶段的 _inject_memory_fs_direct 已用正确的 target_id
        注入了 memory_fs.context，此处不再覆盖。
        """
        try:
            from nbot.memory.fs import get_memory_fs
            mfs = get_memory_fs()
            char_id = identity.character_id
            user_id = identity.target_id
            conv_id = getattr(chat_request, "conversation_id", "") or ""

            ctx_text = mfs.build_prompt_context(
                char_id, user_id, conv_id,
                query=str(getattr(chat_request, "content", "") or ""),
                max_chars=2000,
            )
            if ctx_text:
                # 如果 Pipeline 阶段已注入 memory_fs.context（使用正确的 target_id），
                # 则不覆盖，仅在无 Pipeline 注入时补充
                if not stack.get("memory_fs.context"):
                    stack.add(
                        key="memory_fs_context",
                        content=f"## 角色记忆背景\n{ctx_text}",
                        priority=200,
                        scope="turn",
                    )
                _log.debug("[MemoryFS] injected %d chars for char=%s user=%s",
                           len(ctx_text), char_id, user_id)
        except Exception as exc:
            _log.debug("[MemoryFS] inject failed: %s", exc)

    def _inject_self_correction(self, stack, identity, chat_request) -> None:
        """注入上一轮 Review 生成的自我修正提示（一次性消费，非阻塞）。"""
        try:
            from nbot.character.prompt_stack import PromptStack
            from nbot.review.self_correction import consume_hint

            conv_id = getattr(chat_request, "conversation_id", "") or ""
            hint = consume_hint(identity.character_id, identity.target_id, conv_id)
            if hint:
                stack.add(
                    key="self_correction",
                    content=f"## 自我修正提示\n{hint}",
                    priority=PromptStack.PRIORITY_BEHAVIOR + 1,
                    scope="turn",
                )
                _log.debug("[SelfCorrection] injected hint for char=%s user=%s",
                           identity.character_id, identity.target_id)
        except Exception as exc:
            _log.debug("[SelfCorrection] inject failed: %s", exc)

    def _inject_offline_plot_update(self, stack, identity, chat_request) -> None:
        """注入上一轮 Review 生成的现实时间剧情推进（一次性消费，非阻塞）。"""
        try:
            from nbot.character.prompt_stack import PromptStack
            from nbot.review.models import OfflinePlotUpdate
            from nbot.review.offline_plot import consume_update

            conv_id = getattr(chat_request, "conversation_id", "") or ""
            metadata = getattr(chat_request, "metadata", None)
            if not isinstance(metadata, dict):
                metadata = {}
                chat_request.metadata = metadata
            update = metadata.get("_offline_plot_update")
            if isinstance(update, dict):
                update = OfflinePlotUpdate(**update)
            if not update:
                update = self._build_current_offline_plot_update(identity, chat_request)
                if update:
                    metadata["_offline_plot_update"] = update
                    metadata["_offline_plot_update_current_turn"] = True
            if not update:
                update = consume_update(identity.character_id, identity.target_id, conv_id)
                if update:
                    metadata["_offline_plot_update"] = update
            if update and update.should_inject:
                stack.add(
                    key="plot.real_time_sync",
                    content=update.prompt_text or update.summary,
                    priority=PromptStack.PRIORITY_REACTION_PLAN + 1,
                    scope="turn",
                )
                _log.debug("[OfflinePlot] injected update for char=%s user=%s",
                           identity.character_id, identity.target_id)
        except Exception as exc:
            _log.debug("[OfflinePlot] inject failed: %s", exc)

    def _build_current_offline_plot_update(self, identity, chat_request):
        """Generate the current-turn real-time plot prompt from review rules."""
        try:
            metadata = getattr(chat_request, "metadata", {}) or {}
            if not metadata.get("plot_mode") or not metadata.get("plot_real_time_sync"):
                return None
            from nbot.review.models import ReviewInput
            from nbot.review.rule_review import build_offline_plot_update

            inp = ReviewInput(
                conversation_id=getattr(chat_request, "conversation_id", "") or "",
                character_id=identity.character_id,
                user_id=identity.target_id,
                group_id=getattr(chat_request, "group_id", "") or "",
                user_message=getattr(chat_request, "content", "") or "",
                real_time_context=metadata.get("real_time_context") or {},
                plot_mode=True,
                plot_real_time_sync=True,
            )
            return build_offline_plot_update(inp)
        except Exception as exc:
            _log.debug("[OfflinePlot] current-turn update build failed: %s", exc)
            return None

    def _run_review(self, chat_request, result, turn_context, identity, new_relationship) -> None:
        """执行 Review Pipeline，非阻塞，异常不影响主流程。"""
        try:
            from nbot.review.models import ReviewInput
            from nbot.review.pipeline import get_review_pipeline

            metadata = getattr(chat_request, "metadata", {}) or {}
            selected_choice = metadata.get("selected_plot_choice") or {}

            rel = new_relationship
            rel_state = {}
            if rel:
                rel_state = {
                    "affection": rel.affection,
                    "trust": rel.trust,
                    "familiarity": rel.familiarity,
                    "dependency": rel.dependency,
                    "security": rel.security,
                    "jealousy": rel.jealousy,
                }

            inp = ReviewInput(
                conversation_id=getattr(chat_request, "conversation_id", "") or "",
                character_id=identity.character_id,
                user_id=identity.target_id,
                group_id=getattr(chat_request, "group_id", "") or "",
                user_message=getattr(chat_request, "content", "") or "",
                assistant_message=getattr(result, "final_content", "") or "",
                selected_choice=selected_choice,
                relationship_state=rel_state,
                real_time_context=metadata.get("real_time_context") or {},
                plot_mode=bool(metadata.get("plot_mode")),
                plot_real_time_sync=bool(metadata.get("plot_real_time_sync")),
            )

            # 注入 event_bus 使 review.started / review.finished 事件进入事件流
            event_bus = getattr(self._hook_runtime, "_event_bus", None) if self._hook_runtime else None
            pipeline = get_review_pipeline(event_bus=event_bus)
            output = pipeline.run(inp)

            if not output.skipped:
                self._emit_hook(_E.CHARACTER_MEMORY_REVIEWED, identity, output.to_dict(),
                                chat_request=chat_request)
                # Review 关系变化写入真实关系状态
                self._apply_review_relationship_delta(
                    output, new_relationship, identity, chat_request)

            # MemoryFS 同步不受 skipped 限制——日记每轮都写，记忆按条件写
            self._sync_review_to_memory_fs(inp, output)

            # 根据评分生成下一轮自我修正提示并缓存
            self._store_self_correction_hint(inp, output)
            self._store_offline_plot_update(inp, output, metadata)
        except Exception as exc:
            _log.debug("[CharacterRuntime] review pipeline failed: %s", exc)

    def _store_offline_plot_update(self, inp, output, metadata=None) -> None:
        """缓存 Review 生成的现实时间剧情推进，供下一轮 PromptStack 注入。"""
        try:
            from nbot.review.offline_plot import store_update

            if isinstance(metadata, dict) and metadata.get("_offline_plot_update_current_turn"):
                return
            store_update(
                inp.character_id,
                inp.user_id,
                inp.conversation_id,
                getattr(output, "offline_plot_update", None),
            )
        except Exception as exc:
            _log.debug("[CharacterRuntime] store offline plot update failed: %s", exc)

    def _store_self_correction_hint(self, inp, output) -> None:
        """根据 Review 评分生成自我修正提示，缓存供下一轮 before_turn 注入。"""
        try:
            from nbot.review.self_correction import build_correction_hint, store_hint
            hint = build_correction_hint(output.scores)
            store_hint(inp.character_id, inp.user_id, inp.conversation_id, hint)
        except Exception as exc:
            _log.debug("[CharacterRuntime] store self-correction hint failed: %s", exc)

    def _sync_review_to_memory_fs(self, inp, output) -> None:
        """将 Review 输出同步写入 MemoryFS 逻辑路径（非阻塞）。"""
        try:
            from nbot.memory.fs import get_memory_fs
            mfs = get_memory_fs()
            char_id = inp.character_id
            user_id = inp.user_id
            conv_id = inp.conversation_id

            # Review 不再写入逐轮用户/角色原文；人格记忆和近期摘要由 auto_memory
            # 复用 6 轮一次的模型调用压缩后写入 MemoryFS。
            # 这里只保留剧情选择产生的非逐字剧情摘要。
            if output.plot_update and output.plot_update.should_create_node and conv_id:
                mfs.write(
                    mfs.path_plot(char_id, conv_id),
                    character_id=char_id,
                    target_id=user_id,
                    title=output.plot_update.title or "剧情进展",
                    content=output.plot_update.summary,
                    importance=0.7 if output.plot_update.level == "turning_point" else 0.5,
                    append=True,
                )

        except Exception as exc:
            _log.debug("[CharacterRuntime] memory_fs sync failed: %s", exc)

    def _apply_review_relationship_delta(self, output, new_relationship, identity, chat_request) -> None:
        """将 Review 输出的 relationship_delta 应用到真实关系状态。"""
        delta = getattr(output, "relationship_delta", None)
        if not delta or not new_relationship:
            return
        try:
            _REL_FIELDS = ("affection", "trust", "familiarity", "dependency", "security", "jealousy")
            changed = False
            for field in _REL_FIELDS:
                d = getattr(delta, field, 0)
                if d:
                    old = getattr(new_relationship, field, 0)
                    new = max(0, min(100, old + d))
                    setattr(new_relationship, field, new)
                    changed = True

            if changed and self.relationship_repo:
                self.relationship_repo.save(new_relationship)
                latest_state = None
                if self.state_repo:
                    try:
                        latest_state = self.state_repo.get(
                            identity.character_id, identity.scope_id
                        )
                    except Exception:
                        latest_state = None
                self._record_state_history(
                    latest_state,
                    new_relationship,
                    source="review",
                    reason=str(getattr(delta, "reason", "") or "LLM 复盘关系变化"),
                )
                self._emit_hook(_E.CHARACTER_RELATIONSHIP_CHANGED, identity, {
                    "delta": {f: getattr(delta, f, 0) for f in _REL_FIELDS},
                    "reason": getattr(delta, "reason", ""),
                }, chat_request=chat_request)
                _log.debug("[CharacterRuntime] review relationship delta applied: %s",
                           {f: getattr(delta, f, 0) for f in _REL_FIELDS})
        except Exception as exc:
            _log.debug("[CharacterRuntime] apply review relationship delta failed: %s", exc)
