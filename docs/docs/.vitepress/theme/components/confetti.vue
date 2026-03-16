<script setup lang="ts">
import { onMounted } from 'vue';
import confetti from "canvas-confetti";

onMounted(() => (
   /* 纸屑 */
  confetti({
    particleCount: 100,
    spread: 170,
    origin: { y: 0.6 },
  })
));

</script>
