// 浏览器入口：挂载 Vue 根组件，并注册全局状态容器和样式。
import { createPinia } from "pinia";
import { createApp } from "vue";

import App from "./App.vue";
import "./style.css";

createApp(App).use(createPinia()).mount("#app");

