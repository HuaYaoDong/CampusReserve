FROM node:22-alpine AS build

WORKDIR /web
COPY web/package.json ./
RUN npm install
COPY web ./
RUN npm run build

FROM nginx:1.27-alpine
COPY deploy/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html

