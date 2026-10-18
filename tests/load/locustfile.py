"""Locust 演示脚本；当前只请求健康接口，不代表预约全链路容量。"""

from locust import HttpUser, between, task


class CampusReserveUser(HttpUser):
    wait_time = between(0.2, 1.0)

    @task
    def health_check(self) -> None:
        self.client.get("/api/v1/system/health/live", name="system health")
