from __future__ import annotations
from yandex_cloud_ml_sdk import YCloudML

class YandexGptAPI:
    def __init__(self, folder_id, secret):
        self.sdk = YCloudML(
            folder_id=folder_id,
            auth=secret,
        )

    async def query(self, system, prompt: str) -> str:
        messages = [
            {
                "role": "system",
                "text": system,
            },
            {
                "role": "user",
                "text": prompt,
            },
        ]
        resp = self.sdk.models.completions("yandexgpt").configure(temperature=0.5).run(messages)
        for alternative in resp.alternatives:
            if alternative.role == "assistant":
                return alternative.text
        return resp.alternatives[0].test
