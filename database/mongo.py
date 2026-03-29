import os
from datetime import datetime

import certifi
import motor.motor_asyncio
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


# --- TOURNAMENT SESSION HELPERS ---


async def create_tourney_session():
    """Starts a new tournament session."""
    if db is None:
        return None
    try:
        new_session = {
            "status": "active",
            "start_time": datetime.utcnow(),
            "total_tickets": 0,
            "total_messages": 0,
            "peak_queue": 0,
            "current_queue": 0,
        }
        result = await db.tourney_sessions.insert_one(new_session)
        return result.inserted_id
    except Exception as e:
        print(f"⚠️ DB Error (Create Session): {e}")
        return None


async def get_active_tourney_session():
    """Returns the currently active session document, or None."""
    if db is None:
        return None
    try:
        return await db.tourney_sessions.find_one({"status": "active"})
    except Exception as e:
        print(f"⚠️ DB Error (Get Session): {e}")
        return None


async def end_tourney_session(session_id):
    """Marks the session as finished."""
    if db is None:
        return
    try:
        await db.tourney_sessions.update_one(
            {"_id": session_id},
            {"$set": {"status": "finished", "end_time": datetime.utcnow()}},
        )
    except Exception as e:
        print(f"⚠️ DB Error (End Session): {e}")


async def reset_tourney_session_start_time(session_id):
    """Force-resets an existing active session start_time to now."""
    if db is None:
        return False
    try:
        result = await db.tourney_sessions.update_one(
            {"_id": session_id, "status": "active"},
            {"$set": {"start_time": datetime.utcnow()}},
        )
        return result.modified_count > 0
    except Exception as e:
        print(f"⚠️ DB Error (Reset Session Start Time): {e}")
        return False


async def increment_tourney_message_count(session_id):
    """Increments the global message counter. SILENT FAIL enabled."""
    if db is None:
        return
    try:
        await db.tourney_sessions.update_one(
            {"_id": session_id}, {"$inc": {"total_messages": 1}}
        )
    except Exception as e:
        print(f"⚠️ DB Error (Msg Count): {e}")


async def update_tourney_queue(session_id, change: int):
    """Updates current queue size. SILENT FAIL enabled."""
    if db is None:
        return
    try:
        updated_doc = await db.tourney_sessions.find_one_and_update(
            {"_id": session_id},
            {
                "$inc": {
                    "current_queue": change,
                    "total_tickets": 1 if change > 0 else 0,
                }
            },
            return_document=True,
        )

        if updated_doc:
            current = updated_doc.get("current_queue", 0)
            peak = updated_doc.get("peak_queue", 0)

            if current > peak:
                await db.tourney_sessions.update_one(
                    {"_id": session_id}, {"$set": {"peak_queue": current}}
                )
    except Exception as e:
        print(f"⚠️ DB Error (Update Queue): {e}")


async def increment_staff_closure(session_id, user_id: str, username: str):
    """Tracks staff stats. SILENT FAIL enabled."""
    if db is None:
        return
    try:
        await db.tourney_staff_stats.update_one(
            {"session_id": session_id, "user_id": str(user_id)},
            {"$inc": {"tickets_closed": 1}, "$set": {"username": username}},
            upsert=True,
        )
    except Exception as e:
        print(f"⚠️ DB Error (Staff Closure): {e}")


async def get_top_staff_stats(session_id, limit: int = 12):
    """Fetches the leaderboard."""
    if db is None:
        return []
    try:
        cursor = (
            db.tourney_staff_stats.find({"session_id": session_id})
            .sort("tickets_closed", -1)
            .limit(limit)
        )
        return await cursor.to_list(length=limit)
    except Exception as e:
        print(f"⚠️ DB Error (Get Stats): {e}")
        return []


async def update_matcherino_id(session_id, matcherino_id: str):
    """Saves the Matcherino ID to the active tournament session."""
    if db is None:
        return
    await db.tourney_sessions.update_one(
        {"_id": session_id}, {"$set": {"matcherino_id": matcherino_id}}
    )


async def get_matcherino_id_from_active():
    """Retrieves the Matcherino ID from the currently active session."""
    session = await get_active_tourney_session()
    if session:
        return session.get("matcherino_id")
    return None


# --- BLACKLIST HELPERS ---


async def add_blacklisted_user(
    user_id: str,
    reason: str,
    admin_id: str,
    matcherino: str = None,
    alts: list[str] = None,
):
    """Adds or updates a user in the blacklist."""
    if db is None:
        return

    doc = {
        "_id": user_id,
        "reason": reason,
        "admin_id": admin_id,
        "matcherino": matcherino,
        "alts": alts or [],
        "timestamp": datetime.utcnow(),
    }

    await db.blacklist.replace_one({"_id": user_id}, doc, upsert=True)


async def remove_blacklisted_user(user_id: str):
    """Removes a user from the blacklist."""
    if db is None:
        return
    await db.blacklist.delete_one({"_id": user_id})


async def get_blacklisted_user(user_id: str):
    """Returns the blacklist document if the user is banned, else None."""
    if db is None:
        return None
    return await db.blacklist.find_one({"_id": str(user_id)})


async def get_all_blacklisted_users():
    """Returns a list of all blacklisted users."""
    if db is None:
        return []
    cursor = db.blacklist.find().sort("timestamp", -1)
    return await cursor.to_list(length=None)
