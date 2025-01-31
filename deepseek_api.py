import os

import aiohttp


class DeepSeekAPI:
    def __init__(self):
        self.api_url = "https://api.deepseek.com/v1/chat/completions"
        self.api_key = os.getenv("DEEPSEEK_API_KEY")

    async def query(self, system, prompt: str) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": "deepseek-chat",
            "messages": [{
                "role": "user",
                "content": system+"\nСообщение пользователя: "+prompt
            }],
            "temperature": 0.3
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(self.api_url, json=payload, headers=headers) as response:
                response.raise_for_status()
                data = await response.json()
                return data['choices'][0]['message']['content']