<template>
  <div class="layout">
    <aside v-if="!isLogin" class="side">
      <div class="brand">frpc</div>
      <nav>
        <router-link to="/overview">概览</router-link>
        <router-link to="/mappings">端口映射</router-link>
        <router-link to="/profiles">服务端</router-link>
        <router-link to="/logs">日志</router-link>
        <router-link to="/diagnose">诊断</router-link>
        <router-link to="/backups">备份</router-link>
        <router-link to="/settings">设置</router-link>
      </nav>
    </aside>
    <main class="main"><router-view /></main>
  </div>
</template>
<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
const route = useRoute()
const isLogin = computed(() => route.path === '/login')
</script>
