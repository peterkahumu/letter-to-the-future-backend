import asyncio
from sqlalchemy import text
from app.database import engine

async def main():
    async with engine.begin() as conn:
        res = await conn.execute(text("SELECT count(*) FROM letters;"))
        print(f"Total letters: {res.scalar()}")
        
        res2 = await conn.execute(text("SELECT id, status FROM letters ORDER BY created_at DESC LIMIT 5;"))
        for row in res2:
            print(row)

asyncio.run(main())
