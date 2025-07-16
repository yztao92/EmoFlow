# File: main.py

import os
import logging
import requests
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from jose import jwt, jwk
from jose.utils import base64url_decode
from datetime import datetime, timedelta

from rag.rag_chain import run_rag_chain
from llm.zhipu_llm import zhipu_chat_llm
from llm.emotion_detector import detect_emotion
from dialogue.state_tracker import StateTracker
from models import init_db, SessionLocal, User

from dotenv import load_dotenv
load_dotenv()

# JWT 配置项
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your-fallback-dev-secret")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = 60 * 24 * 7

# Apple 配置
APPLE_PUBLIC_KEYS_URL = "https://appleid.apple.com/auth/keys"
APPLE_ISSUER = "https://appleid.apple.com"
APPLE_CLIENT_ID = "Nick-Studio.EmoFlow"  # 替换为你的服务 ID

# Apple 公钥缓存
apple_keys = []

# 日志配置
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# 检查必要环境变量
required_env_vars = ["ZHIPUAI_API_KEY", "DEEPSEEK_API_KEY"]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    raise ValueError(f"缺少必需的环境变量: {', '.join(missing_vars)}")

# FastAPI 初始化
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# 启动时加载数据库 & Apple 公钥
session_states: Dict[str, StateTracker] = {}

@app.on_event("startup")
def on_startup():
    init_db()
    global apple_keys
    apple_keys = requests.get(APPLE_PUBLIC_KEYS_URL).json()["keys"]
    logger.info("✅ Apple 公钥加载成功")

@app.get("/")
def read_root():
    return {"message": "EmoFlow 服务运行中"}

# ---------------------- Apple 登录认证 ----------------------

class AppleLoginRequest(BaseModel):
    identity_token: str

@app.post("/auth/apple")
def login_with_apple(req: AppleLoginRequest):
    try:
        token = req.identity_token
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header["kid"]
        key_data = next((k for k in apple_keys if k["kid"] == kid), None)
        if not key_data:
            raise HTTPException(status_code=401, detail="Apple 公钥未找到")

        public_key = jwk.construct(key_data)
        message, encoded_sig = token.rsplit('.', 1)
        decoded_sig = base64url_decode(encoded_sig.encode())

        if not public_key.verify(message.encode(), decoded_sig):
            raise HTTPException(status_code=401, detail="无效签名")

        decoded = jwt.decode(
            token,
            key=public_key.to_pem().decode(),
            algorithms=["RS256"],
            audience=APPLE_CLIENT_ID,
            issuer=APPLE_ISSUER
        )

        apple_user_id = decoded["sub"]
        email = decoded.get("email")

        db: Session = SessionLocal()
        user = db.query(User).filter(User.apple_user_id == apple_user_id).first()
        if not user:
            user = User(apple_user_id=apple_user_id, email=email)
            db.add(user)
            db.commit()
            db.refresh(user)

        token_data = {
            "sub": str(user.id),
            "apple_user_id": user.apple_user_id,
            "exp": datetime.utcnow() + timedelta(minutes=JWT_EXPIRE_MINUTES)
        }
        token = jwt.encode(token_data, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)

        return {
            "status": "ok",
            "token": token,
            "user_id": user.id,
            "email": user.email
        }

    except Exception as e:
        logging.error(f"❌ Apple 登录失败: {e}")
        raise HTTPException(status_code=401, detail="Apple 登录验证失败")

def get_current_user(token: str = Header(...)) -> int:
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return int(payload["sub"])
    except Exception:
        raise HTTPException(status_code=401, detail="无效或过期的 Token")

# ---------------------- 聊天模块 ----------------------

class Message(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    session_id: str
    messages: List[Message]

@app.post("/chat")
def chat_with_user(request: ChatRequest) -> Dict[str, Any]:
    logging.info("收到 /chat 请求")
    try:
        logging.info(f"\n🔔 收到请求：{request.json()}")
        state = session_states.setdefault(request.session_id, StateTracker())
        state.history = [(m.role, m.content) for m in request.messages]
        user_query = request.messages[-1].content
        logging.info(f"📨 [用户提问] {user_query}")

        emotion = detect_emotion(user_query)
        state.update_emotion(emotion)
        logging.info(f"🔍 [emotion] 检测到情绪 → {emotion}")

        user_messages = [m for m in request.messages if m.role == "user"]
        round_index = len(user_messages)
        logging.info(f"🔁 [轮次] 用户发言轮次：{round_index}")

        context_summary = state.summary(last_n=3)
        logging.info(f"📝 [状态摘要]\n{context_summary}")

        answer = run_rag_chain(
            query=user_query,
            round_index=round_index,
            state_summary=context_summary
        )

        state.update_message("assistant", answer)

        return {
            "response": {
                "answer": answer,
                "references": []
            }
        }

    except Exception as e:
        logging.error(f"[❌ ERROR] 聊天接口处理失败: {e}")
        return {
            "response": {
                "answer": "抱歉，系统暂时无法处理您的请求，请稍后再试。",
                "references": []
            }
        }

@app.post("/journal/generate")
def generate_journal(request: ChatRequest) -> Dict[str, Any]:
    try:
        logging.info(f"\n📝 收到生成心情日记请求：{request.json()}")
        prompt = "\n".join(f"{m.role}: {m.content}" for m in request.messages)
        system_prompt = f"""你是用户的情绪笔记助手，请根据以下对话内容，以"我"的视角，总结一段今天的心情日记。\n注意要自然、有情感，不要提到对话或 AI，只写个人的感受和经历：\n-----------\n{prompt}\n-----------"""

        result = zhipu_chat_llm(system_prompt)
        journal = result.get("answer", "今天的心情有点复杂，暂时说不清楚。")

        return {"journal": journal}

    except Exception as e:
        logging.error(f"[❌ ERROR] 心情日记生成失败: {e}")
        return {"journal": "生成失败"}