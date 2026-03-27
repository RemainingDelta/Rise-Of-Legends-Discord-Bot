import os
import motor.motor_asyncio
import certifi
from dotenv import load_dotenv

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")

if not MONGO_URI:
    print("⚠️ CRITICAL: MONGO_URI missing in .env")
    db = None
else:
    try:
        client = motor.motor_asyncio.AsyncIOMotorClient(
            MONGO_URI, tlsCAFile=certifi.where()
        )
        db = client["rol_bot_db"]
        print("✅ Connected to Cloud Database (MongoDB)")
    except Exception as e:
        print(f"❌ DB Connection Error: {e}")
        db = None
