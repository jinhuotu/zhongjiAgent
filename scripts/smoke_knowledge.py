import asyncio
import json

import httpx


async def main() -> None:
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=60.0) as client:
        login = await client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "Admin@123456"},
        )
        print("login", login.status_code)
        login.raise_for_status()
        token = login.json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        bases = await client.get("/api/v1/knowledge/bases", headers=headers)
        print("bases", bases.status_code)
        bases.raise_for_status()
        items = (bases.json().get("data") or {}).get("items") or []
        if items:
            base_id = items[0]["id"]
            print("use existing base", base_id, items[0].get("name"))
        else:
            created_base = await client.post(
                "/api/v1/knowledge/bases",
                json={"name": "冒烟测试库", "description": "smoke"},
                headers=headers,
            )
            print("create base", created_base.status_code)
            created_base.raise_for_status()
            base_id = created_base.json()["data"]["item"]["id"]

        body = {
            "baseId": base_id,
            "title": "车式窑空燃比说明",
            "content": (
                "工业燃气车式窑（车底炉）标准空燃比通常按过量空气系数控制。"
                "天然气燃烧时，理论空燃比约 9.5~10.5，实际运行常用 lambda 1.05~1.20，并监测残氧 O2。"
                "炉温、炉压与空燃比需联动调节，避免还原气氛导致结碳或氧化烧损。"
            ),
            "tags": ["工艺", "空燃比"],
            "uploader": "中机六院管理员",
        }
        created = await client.post(
            "/api/v1/knowledge/documents/from-text",
            json=body,
            headers=headers,
        )
        print("from-text", created.status_code)
        print(created.text[:1000])
        created.raise_for_status()

        listed = await client.get(
            f"/api/v1/knowledge/documents?baseId={base_id}",
            headers=headers,
        )
        print("list", listed.status_code, "items", len(listed.json().get("items") or []))

        searched = await client.post(
            "/api/v1/knowledge/search",
            json={"query": "车式窑空燃比是多少", "baseId": base_id, "topK": 3},
            headers=headers,
        )
        print("search", searched.status_code)
        print(json.dumps(searched.json(), ensure_ascii=False)[:1000])


if __name__ == "__main__":
    asyncio.run(main())
