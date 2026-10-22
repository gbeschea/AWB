"""Update all SKU costs to 10."""
import asyncio
from sqlalchemy import text
from app.core.database import AsyncSessionLocal


async def update_costs():
    async with AsyncSessionLocal() as db:
        result = await db.execute(text("UPDATE sku_costs SET cost = 10"))
        await db.commit()
        print(f"Updated all SKU costs to 10 RON")
        
        # Show count
        count_result = await db.execute(text("SELECT COUNT(*) FROM sku_costs"))
        count = count_result.scalar()
        print(f"Total SKUs in database: {count}")


if __name__ == "__main__":
    asyncio.run(update_costs())
