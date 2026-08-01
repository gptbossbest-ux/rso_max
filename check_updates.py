import httpx, json, os
from dotenv import load_dotenv
load_dotenv()
TOKEN = os.getenv('TOKEN')
resp = httpx.get(
    'https://platform-api.max.ru/updates',
    headers={'Authorization': TOKEN},
    params={'timeout': 5},
    timeout=10
)
print(json.dumps(resp.json(), indent=2, ensure_ascii=False))