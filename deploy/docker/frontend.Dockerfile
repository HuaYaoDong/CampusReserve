# 构建阶段把 Vue/TypeScript 编译成静态文件。
FROM node:22-alpine AS build

WORKDIR /web
COPY web/package.json ./
RUN npm install
COPY web ./
RUN npm run build

# 运行阶段只保留 Nginx 和构建产物，不携带 node_modules。
FROM nginx:1.27-alpine
COPY deploy/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html

