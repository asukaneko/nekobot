<script setup lang="ts">
import { onMounted, ref } from 'vue'

const progress = ref(0)
const showButton = ref(false)

const scrollToTop = () => {
  window.scrollTo({
    top: 0,
    behavior: 'smooth'
  })
}

onMounted(() => {
  window.addEventListener('scroll', () => {
    const scrollHeight = document.documentElement.scrollHeight - document.documentElement.clientHeight
    progress.value = (window.scrollY / scrollHeight) * 100
    showButton.value = window.scrollY > 300
  })
})
</script>

<template>
  <DefaultTheme.Layout />
  
  <div 
    v-if="showButton" 
    class="progress-button"
    @click="scrollToTop"
  >
    <svg class="progress-circle" viewBox="0 0 36 36">
      <path
        class="progress-circle-bg"
        d="M18 2.0845
          a 15.9155 15.9155 0 0 1 0 31.831
          a 15.9155 15.9155 0 0 1 0 -31.831"
      />
      <path
        class="progress-circle-fill"
        :stroke-dasharray="`${progress}, 100`"
        d="M18 2.0845
          a 15.9155 15.9155 0 0 1 0 31.831
          a 15.9155 15.9155 0 0 1 0 -31.831"
      />
    </svg>
    <span class="arrow">↑</span>
  </div>
</template>

<style scoped>
.progress-button {
  position: fixed;
  right: 20px;
  bottom: 20px;
  width: 40px;
  height: 40px;
  cursor: pointer;
  z-index: 99;
}

.progress-circle {
  width: 100%;
  height: 100%;
  transform: rotate(-90deg);
}

.progress-circle-bg {
  fill: none;
  stroke: var(--vp-c-bg-soft);
  stroke-width: 3;
}

.progress-circle-fill {
  fill: none;
  stroke: var(--vp-c-brand);
  stroke-width: 3;
  stroke-linecap: round;
  transition: stroke-dasharray 0.1s linear;
}

.arrow {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  font-size: 16px;
  color: var(--vp-c-text-1);
}
</style>