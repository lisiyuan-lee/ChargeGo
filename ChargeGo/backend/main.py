"""
ChargeGo — 智能充电桩调度计费系统 (后端服务)
==============================================
直接运行:  python3 main.py
API 文档:  http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Literal, Optional, Union

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, create_engine, or_, and_
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

DATABASE_URL = "sqlite:///chargego.db"
FAST_PILE_COUNT = 3      # 快充桩数量
SLOW_PILE_COUNT = 2      # 慢充桩数量
WAITING_AREA_SIZE = 5    # 等候区容量
CHARGING_QUEUE_LEN = 2   # 每桩队列长度
FAST_POWER = 30.0        # 快充功率 (kWh/h)
SLOW_POWER = 10.0        # 慢充功率 (kWh/h)
SERVICE_FEE_RATE = 0.8   # 服务费率 (元/kWh)
SPEED_MULTIPLIER = 20    # 时间加速倍率（演示用）

# ═══════════════════════════════════════════════════════════════
# 数据库
# ═══════════════════════════════════════════════════════════════

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ═══════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    role = Column(String(16), nullable=False, default="user")
    nickname = Column(String(64), nullable=True)
    car_id = Column(String(32), nullable=True)  # 车牌号
    battery_capacity = Column(Float, nullable=False, default=60.0)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

class ChargingPile(Base):
    __tablename__ = "charging_piles"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(16), unique=True, nullable=False, index=True)
    type = Column(String(16), nullable=False)               # fast / slow
    power = Column(Float, nullable=False)
    is_working = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

class ChargeRequest(Base):
    __tablename__ = "charge_requests"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    pile_id = Column(Integer, ForeignKey("charging_piles.id"), nullable=True, index=True)
    mode = Column(String(16), nullable=False)                # fast / slow
    requested_energy = Column(Float, nullable=False)         # kWh
    status = Column(String(24), nullable=False, default="waiting", index=True)
    queue_number = Column(String(16), nullable=False, index=True)
    waiting_start_time = Column(DateTime, nullable=False, default=datetime.now)
    assign_time = Column(DateTime, nullable=True)
    start_time = Column(DateTime, nullable=True)
    finish_time = Column(DateTime, nullable=True)
    paused_energy = Column(Float, nullable=False, default=0.0)  # 暂停时已充电量
    created_at = Column(DateTime, nullable=False, default=datetime.now)

class ChargeBill(Base):
    __tablename__ = "charge_bills"
    id = Column(Integer, primary_key=True, index=True)
    request_id = Column(Integer, ForeignKey("charge_requests.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    pile_id = Column(Integer, ForeignKey("charging_piles.id"), nullable=False, index=True)
    generated_at = Column(DateTime, nullable=False, default=datetime.now)
    start_time = Column(DateTime, nullable=False)
    stop_time = Column(DateTime, nullable=False)
    charge_energy = Column(Float, nullable=False)
    charge_duration = Column(Float, nullable=False)          # 小时
    electricity_fee = Column(Float, nullable=False)
    service_fee = Column(Float, nullable=False)
    total_fee = Column(Float, nullable=False)

# ═══════════════════════════════════════════════════════════════
# Pydantic 请求体
# ═══════════════════════════════════════════════════════════════

class RegisterRequest(BaseModel):
    username: str
    password: str
    nickname: Optional[str] = None
    car_id: Optional[str] = None  # 车牌号
    battery_capacity: float = Field(default=60.0, gt=0)

    @field_validator("username")
    @classmethod
    def check_username(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("用户名不能为空")
        if len(v) < 3:
            raise ValueError("用户名至少需要 3 个字符")
        if len(v) > 64:
            raise ValueError("用户名不能超过 64 个字符")
        return v

    @field_validator("password")
    @classmethod
    def check_password(cls, v: str) -> str:
        if not v:
            raise ValueError("密码不能为空")
        if len(v) < 2:
            raise ValueError("密码至少需要 2 个字符")
        if len(v) > 128:
            raise ValueError("密码不能超过 128 个字符")
        return v

class LoginRequest(BaseModel):
    username: str
    password: str

class ChargeSubmitRequest(BaseModel):
    mode: Literal["fast", "slow"]
    requested_energy: float = Field(gt=0)

class ChargeModifyRequest(BaseModel):
    mode: Optional[Literal["fast", "slow"]] = None
    requested_energy: Optional[float] = Field(default=None, gt=0)

class PileControlRequest(BaseModel):
    pile_id: int

class FaultRequest(BaseModel):
    pile_id: int
    strategy: Literal["priority", "sequence", "shortest"] = "sequence"

class PileConfigRequest(BaseModel):
    pile_id: int
    power: Optional[float] = Field(default=None, gt=0)

class RuntimeConfigRequest(BaseModel):
    waiting_area_size: int = Field(gt=0, le=100)
    charging_queue_len: int = Field(gt=0, le=20)

# ═══════════════════════════════════════════════════════════════
# 安全模块（密码 + Token）
# ═══════════════════════════════════════════════════════════════

TOKENS: dict[str, int] = {}  # token -> user_id

def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return f"{salt}${digest}"

def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, _ = stored_hash.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(password, salt), stored_hash)

def create_token(user: User) -> str:
    token = secrets.token_urlsafe(32)
    TOKENS[token] = user.id
    return token

def get_current_user(
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = authorization.split(" ", 1)[1]
    user_id = TOKENS.get(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="登录已失效")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    return user

def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return current_user

# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

def scaled_elapsed_hours(start_time: datetime, now: Optional[datetime] = None) -> float:
    """时间加速后的流逝小时数"""
    now = now or datetime.now()
    return max((now - start_time).total_seconds() / 3600 * SPEED_MULTIPLIER, 0)

def progress_for_request(request: ChargeRequest, pile: Optional[ChargingPile]) -> dict:
    """计算充电进度，支持暂停后累加"""
    power = pile.power if pile else None
    estimated_hours = request.requested_energy / power if power else 0
    charged_energy = request.paused_energy or 0.0
    percent = min(100.0, charged_energy / request.requested_energy * 100) if request.requested_energy else 0.0
    remaining_hours = max(estimated_hours - (charged_energy / power if power else 0), 0)

    if request.status == "charging" and request.start_time and power:
        elapsed = scaled_elapsed_hours(request.start_time)
        charged_energy = min(request.requested_energy, charged_energy + elapsed * power)
        percent = min(100.0, charged_energy / request.requested_energy * 100)
        remaining_hours = max(estimated_hours - (charged_energy / power), 0)
    elif request.status == "paused":
        percent = min(100.0, charged_energy / request.requested_energy * 100) if request.requested_energy else 0.0
        remaining_hours = max(estimated_hours - (charged_energy / power if power else 0), 0)
    elif request.status == "completed":
        charged_energy = request.requested_energy
        percent = 100.0
        remaining_hours = 0

    return {
        "percent": round(percent, 1),
        "charged_energy": round(charged_energy, 2),
        "remaining_minutes": round(remaining_hours * 60, 1),
        "estimated_minutes": round(estimated_hours * 60, 1),
        "power": power,
        "speed_multiplier": SPEED_MULTIPLIER,
    }

def user_to_dict(user: User) -> dict:
    return {
        "id": user.id, "username": user.username, "role": user.role,
        "nickname": user.nickname, "car_id": user.car_id,
        "battery_capacity": user.battery_capacity,
        "created_at": user.created_at,
    }

def request_to_dict(r: ChargeRequest, progress: Optional[dict] = None) -> dict:
    d = {
        "id": r.id, "user_id": r.user_id, "pile_id": r.pile_id,
        "mode": r.mode, "requested_energy": r.requested_energy,
        "status": r.status, "queue_number": r.queue_number,
        "waiting_start_time": r.waiting_start_time, "assign_time": r.assign_time,
        "start_time": r.start_time, "finish_time": r.finish_time,
        "created_at": r.created_at,
    }
    if progress is not None:
        d["progress"] = progress
    return d

def bill_to_dict(b: ChargeBill) -> dict:
    return {
        "id": b.id, "request_id": b.request_id, "user_id": b.user_id,
        "pile_id": b.pile_id, "generated_at": b.generated_at,
        "start_time": b.start_time, "stop_time": b.stop_time,
        "charge_energy": b.charge_energy, "charge_duration": b.charge_duration,
        "electricity_fee": b.electricity_fee, "service_fee": b.service_fee,
        "total_fee": b.total_fee,
    }

def pile_to_dict(p: ChargingPile) -> dict:
    return {
        "id": p.id, "code": p.code, "type": p.type,
        "power": p.power, "is_working": p.is_working,
        "created_at": p.created_at,
    }

# ═══════════════════════════════════════════════════════════════
# 计费逻辑（分时电价）
# ═══════════════════════════════════════════════════════════════

def electricity_rate(moment: datetime) -> float:
    """分时电价：峰时1.0  平时0.7  谷时0.4"""
    h = moment.hour
    if 10 <= h < 15 or 18 <= h < 21:
        return 1.0   # 峰时
    if 7 <= h < 10 or 15 <= h < 18 or 21 <= h < 23:
        return 0.7   # 平时
    return 0.4       # 谷时

def calculate_bill(start: datetime, stop: datetime, energy: float) -> dict:
    """按分时电价分段计算费用"""
    duration = (stop - start).total_seconds() / 3600
    avg_rate = electricity_rate(start)  # 简化取起始时段电价
    electricity_fee = round(energy * avg_rate, 2)
    service_fee = round(energy * SERVICE_FEE_RATE, 2)
    return {
        "charge_energy": round(energy, 2),
        "charge_duration": round(duration, 4),
        "electricity_fee": electricity_fee,
        "service_fee": service_fee,
        "total_fee": round(electricity_fee + service_fee, 2),
    }

def create_bill(db: Session, request: ChargeRequest, stop_time: datetime,
                elapsed_hours_override: Optional[float] = None,
                final_status: str = "completed") -> ChargeBill:
    """为充电请求生成账单，支持暂停后结算"""
    pile = db.query(ChargingPile).filter(ChargingPile.id == request.pile_id).first()
    if request.status == "paused":
        energy = request.paused_energy or 0.0
    elif elapsed_hours_override is not None:
        energy = min(request.requested_energy, elapsed_hours_override * (pile.power if pile else 0))
    elif request.start_time:
        energy = min(request.requested_energy, scaled_elapsed_hours(request.start_time) * (pile.power if pile else 0))
    else:
        energy = request.paused_energy or 0.0
    calc = calculate_bill(request.start_time or request.created_at, stop_time, energy)

    bill = ChargeBill(
        request_id=request.id, user_id=request.user_id, pile_id=request.pile_id,
        start_time=request.start_time or request.created_at, stop_time=stop_time,
        charge_energy=calc["charge_energy"],
        charge_duration=calc["charge_duration"],
        electricity_fee=calc["electricity_fee"],
        service_fee=calc["service_fee"],
        total_fee=calc["total_fee"],
    )
    db.add(bill)
    request.status = final_status
    request.finish_time = stop_time
    db.add(request)
    return bill

# ═══════════════════════════════════════════════════════════════
# 调度器
# ═══════════════════════════════════════════════════════════════

ACTIVE_STATUSES = ("waiting", "fault_waiting", "queued", "charging", "paused")

def pile_load(db: Session, pile: ChargingPile) -> int:
    """计算某桩当前负载（充电中 + 排队）"""
    return db.query(ChargeRequest).filter(
        ChargeRequest.pile_id == pile.id,
        ChargeRequest.status.in_(("charging", "queued")),
    ).count()

def settle_completed_charges(db: Session) -> None:
    """自动结算已充满的车辆"""
    charging_requests = db.query(ChargeRequest).filter(
        ChargeRequest.status == "charging"
    ).all()
    now = datetime.now()
    for req in charging_requests:
        pile = db.query(ChargingPile).filter(ChargingPile.id == req.pile_id).first()
        if not pile:
            continue
        elapsed = scaled_elapsed_hours(req.start_time, now)
        if elapsed * pile.power >= req.requested_energy:
            create_bill(db, req, now)

def start_next_vehicle(db: Session, pile: ChargingPile) -> bool:
    """从队列中启动下一辆车"""
    next_req = db.query(ChargeRequest).filter(
        ChargeRequest.pile_id == pile.id, ChargeRequest.status == "queued"
    ).order_by(ChargeRequest.assign_time.asc(), ChargeRequest.id.asc()).first()
    if not next_req:
        return False
    next_req.status = "charging"
    next_req.start_time = datetime.now()
    db.add(next_req)
    return True

def eligible_piles(db: Session, mode: str) -> list[ChargingPile]:
    """获取可接受新车的充电桩（运行中 + 未满）"""
    return [
        p for p in db.query(ChargingPile)
        .filter(ChargingPile.type == mode, ChargingPile.is_working == True)
        .order_by(ChargingPile.id.asc()).all()
        if pile_load(db, p) < CHARGING_QUEUE_LEN
    ]

def advance_active_queues(db: Session) -> None:
    """推进排队队列"""
    settle_completed_charges(db)
    for pile in db.query(ChargingPile).all():
        if not pile.is_working:
            continue
        charging = db.query(ChargeRequest).filter(
            ChargeRequest.pile_id == pile.id, ChargeRequest.status == "charging"
        ).first()
        if not charging:
            start_next_vehicle(db, pile)

def run_scheduler(db: Session) -> None:
    """主调度器：把等候区车辆分配到充电桩 + 推进队列"""
    settle_completed_charges(db)

    # 推进已有队列
    for pile in db.query(ChargingPile).all():
        if pile.is_working:
            charging = db.query(ChargeRequest).filter(
                ChargeRequest.pile_id == pile.id, ChargeRequest.status == "charging"
            ).first()
            if not charging:
                start_next_vehicle(db, pile)

    # 分配等候区车辆
    for mode in ("fast", "slow"):
        waiting = db.query(ChargeRequest).filter(
            ChargeRequest.mode == mode, ChargeRequest.status == "waiting"
        ).order_by(ChargeRequest.waiting_start_time.asc(), ChargeRequest.id.asc()).all()

        for req in waiting:
            piles = eligible_piles(db, mode)
            if not piles:
                break
            # 选择负载最小的桩
            best = min(piles, key=lambda p: pile_load(db, p))
            req.pile_id = best.id
            req.status = "queued"
            req.assign_time = datetime.now()
            db.add(req)
            if not db.query(ChargeRequest).filter(
                ChargeRequest.pile_id == best.id, ChargeRequest.status == "charging"
            ).first():
                start_next_vehicle(db, best)

    # 处理故障等待车辆
    for mode in ("fast", "slow"):
        fault_waiting = db.query(ChargeRequest).filter(
            ChargeRequest.mode == mode, ChargeRequest.status == "fault_waiting"
        ).all()
        if not fault_waiting:
            continue
        working_piles = db.query(ChargingPile).filter(
            ChargingPile.type == mode, ChargingPile.is_working == True
        ).all()
        for req in fault_waiting:
            available = [p for p in working_piles if pile_load(db, p) < CHARGING_QUEUE_LEN]
            if not available:
                break
            best = min(available, key=lambda p: pile_load(db, p))
            req.pile_id = best.id
            req.status = "queued"
            req.assign_time = datetime.now()
            db.add(req)
            if not db.query(ChargeRequest).filter(
                ChargeRequest.pile_id == best.id, ChargeRequest.status == "charging"
            ).first():
                start_next_vehicle(db, best)

    db.commit()

# ═══════════════════════════════════════════════════════════════
# 故障调度
# ═══════════════════════════════════════════════════════════════

_last_fault_record: Optional[dict] = None

def stop_fault_pile(db: Session, pile: ChargingPile, strategy: str) -> dict:
    """关闭故障桩，将其队列车辆重新分配"""
    global _last_fault_record
    pile.is_working = False
    db.add(pile)

    # 收集受影响车辆
    affected = db.query(ChargeRequest).filter(
        ChargeRequest.pile_id == pile.id,
        ChargeRequest.status.in_(("charging", "queued")),
    ).all()

    participants = []
    assignments = []

    for req in affected:
        if req.status == "charging":
            create_bill(db, req, datetime.now(), final_status="completed" if
                       scaled_elapsed_hours(req.start_time) * pile.power >= req.requested_energy
                       else "cancelled")
        else:
            req.status = "fault_waiting"
            req.pile_id = None
            req.assign_time = None
            db.add(req)
        participants.append({"request_id": req.id, "queue_number": req.queue_number,
                            "source_status": req.status})

    db.commit()

    # 按策略重新分配
    fault_vehicles = db.query(ChargeRequest).filter(
        ChargeRequest.mode == pile.type, ChargeRequest.status == "fault_waiting"
    ).order_by(
        ChargeRequest.waiting_start_time.asc() if strategy == "sequence"
        else ChargeRequest.id.asc()
    ).all()

    working = db.query(ChargingPile).filter(
        ChargingPile.type == pile.type, ChargingPile.is_working == True
    ).all()

    for req in fault_vehicles:
        available = [p for p in working if pile_load(db, p) < CHARGING_QUEUE_LEN]
        if not available:
            break
        if strategy == "shortest":
            # 最短时长策略：选能最快完成该车辆充电的桩
            target = min(available, key=lambda p: req.requested_energy / p.power + pile_load(db, p) * 0.5)
        else:
            target = min(available, key=lambda p: pile_load(db, p))
        req.pile_id = target.id
        req.status = "queued"
        req.assign_time = datetime.now()
        db.add(req)
        assignments.append({"request_id": req.id, "queue_number": req.queue_number,
                           "pile_code": target.code})
        if not db.query(ChargeRequest).filter(
            ChargeRequest.pile_id == target.id, ChargeRequest.status == "charging"
        ).first():
            start_next_vehicle(db, target)

    db.commit()

    strategy_names = {"priority": "故障队列优先", "sequence": "时间顺序调度", "shortest": "最短时长调度"}
    _last_fault_record = {
        "fault_pile_code": pile.code,
        "strategy": strategy_names.get(strategy, strategy),
        "message": f"已关闭 {pile.code}({strategy_names.get(strategy)}), 重新分配 {len(assignments)} 辆车",
        "participants": participants,
        "assignments": assignments,
    }
    return _last_fault_record

def recover_pile(db: Session, pile: ChargingPile) -> dict:
    """恢复充电桩运行"""
    pile.is_working = True
    db.add(pile)
    db.commit()
    run_scheduler(db)
    return {"message": f"{pile.code} 已恢复运行"}

# ═══════════════════════════════════════════════════════════════
# API 路由
# ═══════════════════════════════════════════════════════════════

app = FastAPI(title="ChargeGo API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 认证 ───────────────────────────────────────────────────────

@app.post("/api/register")
def api_register(payload: RegisterRequest, db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == payload.username).first():
        raise HTTPException(status_code=400, detail="用户名已存在")
    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role="user",
        nickname=payload.nickname or payload.username,
        car_id=payload.car_id,
        battery_capacity=payload.battery_capacity,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"token": create_token(user), "user": user_to_dict(user)}

@app.post("/api/login")
def api_login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == payload.username).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=400, detail="用户名或密码错误")
    return {"token": create_token(user), "user": user_to_dict(user)}

@app.get("/api/me")
def api_me(current_user: User = Depends(get_current_user)):
    return user_to_dict(current_user)

# ── 用户充电操作 ───────────────────────────────────────────────

def _active_request(db: Session, user_id: int) -> Optional[ChargeRequest]:
    return db.query(ChargeRequest).filter(
        ChargeRequest.user_id == user_id,
        ChargeRequest.status.in_(ACTIVE_STATUSES),
    ).order_by(ChargeRequest.id.desc()).first()

def _gen_queue_number(db: Session, mode: str) -> str:
    prefix = "F" if mode == "fast" else "T"
    nums = []
    for (qn,) in db.query(ChargeRequest.queue_number).filter(
        ChargeRequest.queue_number.like(f"{prefix}%")
    ).all():
        try:
            nums.append(int(qn[1:]))
        except (TypeError, ValueError):
            continue
    return f"{prefix}{max(nums, default=0) + 1}"

@app.post("/api/charge/submit")
def charge_submit(payload: ChargeSubmitRequest, user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    if user.role != "user":
        raise HTTPException(status_code=403, detail="管理员不能提交充电请求")
    if payload.requested_energy > user.battery_capacity:
        raise HTTPException(status_code=400, detail="请求电量不能超过电池容量")
    if _active_request(db, user.id):
        raise HTTPException(status_code=400, detail="当前已有未完成的充电请求")
    if db.query(ChargeRequest).filter(ChargeRequest.status == "waiting").count() >= WAITING_AREA_SIZE:
        raise HTTPException(status_code=400, detail="等候区已满")

    req = ChargeRequest(
        user_id=user.id, mode=payload.mode,
        requested_energy=payload.requested_energy,
        queue_number=_gen_queue_number(db, payload.mode),
        status="waiting", waiting_start_time=datetime.now(),
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    run_scheduler(db)
    db.refresh(req)
    return request_to_dict(req)

@app.put("/api/charge/modify")
def charge_modify(payload: ChargeModifyRequest, user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    req = _active_request(db, user.id)
    if not req:
        raise HTTPException(status_code=404, detail="没有可修改的充电请求")
    if req.status != "waiting":
        raise HTTPException(status_code=400, detail="只有等候区请求允许修改")
    if payload.requested_energy is None and payload.mode is None:
        raise HTTPException(status_code=400, detail="没有提交修改内容")
    if payload.requested_energy and payload.requested_energy > user.battery_capacity:
        raise HTTPException(status_code=400, detail="请求电量不能超过电池容量")

    if payload.mode and payload.mode != req.mode:
        req.mode = payload.mode
        req.queue_number = _gen_queue_number(db, payload.mode)
        req.waiting_start_time = datetime.now()
    if payload.requested_energy is not None:
        req.requested_energy = payload.requested_energy

    db.add(req)
    db.commit()
    run_scheduler(db)
    db.refresh(req)
    return request_to_dict(req)

@app.delete("/api/charge/cancel")
def charge_cancel(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    req = _active_request(db, user.id)
    if not req:
        raise HTTPException(status_code=404, detail="没有可取消的充电请求")

    if req.status == "charging":
        create_bill(db, req, datetime.now(),
                    elapsed_hours_override=scaled_elapsed_hours(req.start_time),
                    final_status="cancelled")
    else:
        req.status = "cancelled"
        req.finish_time = datetime.now()
        db.add(req)
    db.commit()
    run_scheduler(db)
    return {"message": "已取消", "request": request_to_dict(req)}

@app.post("/api/charge/pause")
def charge_pause(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """暂停充电：冻结进度，保留已充电量"""
    req = _active_request(db, user.id)
    if not req:
        raise HTTPException(status_code=404, detail="没有进行中的充电请求")
    if req.status != "charging":
        raise HTTPException(status_code=400, detail="只有充电中才能暂停")
    pile = db.query(ChargingPile).filter(ChargingPile.id == req.pile_id).first()
    elapsed = scaled_elapsed_hours(req.start_time)
    req.paused_energy = min(req.requested_energy, elapsed * (pile.power if pile else 0))
    req.status = "paused"
    req.start_time = None
    db.add(req)
    db.commit()
    return {"message": "充电已暂停", "paused_energy": round(req.paused_energy, 2)}

@app.post("/api/charge/resume")
def charge_resume(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """恢复充电：从暂停处继续"""
    req = _active_request(db, user.id)
    if not req:
        raise HTTPException(status_code=404, detail="没有可恢复的充电请求")
    if req.status != "paused":
        raise HTTPException(status_code=400, detail="只有暂停状态才能恢复")
    req.status = "charging"
    req.start_time = datetime.now()
    db.add(req)
    db.commit()
    run_scheduler(db)
    return {"message": "充电已恢复", "request": request_to_dict(req)}

@app.post("/api/charge/finish")
def charge_finish(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    req = _active_request(db, user.id)
    if not req:
        raise HTTPException(status_code=404, detail="没有进行中的充电请求")
    if req.status not in ("charging", "paused"):
        raise HTTPException(status_code=400, detail="车辆尚未开始充电")
    override = scaled_elapsed_hours(req.start_time) if req.start_time else None
    bill = create_bill(db, req, datetime.now(), elapsed_hours_override=override)
    db.commit()
    run_scheduler(db)
    return {"message": "充电已结束", "bill": bill_to_dict(bill)}

@app.get("/api/charge/status")
def charge_status(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    run_scheduler(db)
    req = _active_request(db, user.id)
    if not req:
        return {"active": False, "request": None, "front_count": 0, "area": None}
    pile = db.query(ChargingPile).filter(ChargingPile.id == req.pile_id).first() if req.pile_id else None
    return {
        "active": True,
        "request": request_to_dict(req, progress_for_request(req, pile)),
        "front_count": _front_count(db, req),
        "area": {"waiting": "等候区", "fault_waiting": "故障等待", "queued": "充电队列",
                 "charging": "充电中"}.get(req.status, "-"),
    }

def _front_count(db: Session, req: ChargeRequest) -> int:
    """计算排在前面的车辆数"""
    if req.status in ("waiting", "fault_waiting"):
        return db.query(ChargeRequest).filter(
            ChargeRequest.mode == req.mode,
            ChargeRequest.status == req.status,
            or_(
                ChargeRequest.waiting_start_time < req.waiting_start_time,
                and_(ChargeRequest.waiting_start_time == req.waiting_start_time,
                     ChargeRequest.id < req.id),
            ),
        ).count()
    if req.status == "queued":
        return db.query(ChargeRequest).filter(
            ChargeRequest.pile_id == req.pile_id,
            ChargeRequest.status.in_(("charging", "queued")),
            ChargeRequest.id != req.id,
        ).count()
    return 0

@app.get("/api/charge/bills")
def charge_bills(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    run_scheduler(db)
    q = db.query(ChargeBill).order_by(ChargeBill.generated_at.desc())
    if user.role != "admin":
        q = q.filter(ChargeBill.user_id == user.id)
    return [bill_to_dict(b) for b in q.all()]

# ── 管理员操作 ─────────────────────────────────────────────────

@app.get("/api/admin/users")
def admin_users(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return [user_to_dict(u) for u in db.query(User).order_by(User.id.asc()).all()]

@app.get("/api/admin/config")
def admin_get_config(admin: User = Depends(require_admin)):
    return {
        "fast_pile_count": FAST_PILE_COUNT, "slow_pile_count": SLOW_PILE_COUNT,
        "waiting_area_size": WAITING_AREA_SIZE, "charging_queue_len": CHARGING_QUEUE_LEN,
        "fast_power": FAST_POWER, "slow_power": SLOW_POWER,
        "service_fee_rate": SERVICE_FEE_RATE,
    }

@app.put("/api/admin/config")
def admin_update_config(payload: RuntimeConfigRequest, admin: User = Depends(require_admin)):
    global WAITING_AREA_SIZE, CHARGING_QUEUE_LEN
    WAITING_AREA_SIZE = payload.waiting_area_size
    CHARGING_QUEUE_LEN = payload.charging_queue_len
    return admin_get_config(admin)

@app.get("/api/admin/piles/status")
def admin_piles_status(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    run_scheduler(db)
    result = []
    for pile in db.query(ChargingPile).order_by(ChargingPile.id.asc()).all():
        data = pile_to_dict(pile)
        bills = db.query(ChargeBill).filter(ChargeBill.pile_id == pile.id).all()
        current = db.query(ChargeRequest).filter(
            ChargeRequest.pile_id == pile.id, ChargeRequest.status == "charging"
        ).first()
        queued = db.query(ChargeRequest).filter(
            ChargeRequest.pile_id == pile.id, ChargeRequest.status == "queued"
        ).order_by(ChargeRequest.assign_time.asc()).all()
        data.update({
            "charge_count": len(bills),
            "total_duration": round(sum(b.charge_duration for b in bills), 4),
            "total_energy": round(sum(b.charge_energy for b in bills), 2),
            "total_fee": round(sum(b.total_fee for b in bills), 2),
            "current": _request_with_user(db, current) if current else None,
            "queued": [_request_with_user(db, r) for r in queued],
        })
        result.append(data)
    return result

def _request_with_user(db: Session, req: ChargeRequest) -> dict:
    user = db.query(User).filter(User.id == req.user_id).first()
    pile = db.query(ChargingPile).filter(ChargingPile.id == req.pile_id).first() if req.pile_id else None
    d = request_to_dict(req, progress_for_request(req, pile))
    d["username"] = user.username if user else None
    d["nickname"] = user.nickname if user else None
    d["user_id"] = user.id if user else None
    d["battery_capacity"] = user.battery_capacity if user else None
    d["queue_wait_minutes"] = round(max((datetime.now() - req.waiting_start_time).total_seconds() / 60, 0), 1)
    return d

@app.get("/api/admin/piles/queues")
def admin_piles_queues(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    run_scheduler(db)
    result = []
    for pile in db.query(ChargingPile).order_by(ChargingPile.id.asc()).all():
        current = db.query(ChargeRequest).filter(
            ChargeRequest.pile_id == pile.id, ChargeRequest.status == "charging"
        ).first()
        queued = db.query(ChargeRequest).filter(
            ChargeRequest.pile_id == pile.id, ChargeRequest.status == "queued"
        ).order_by(ChargeRequest.assign_time.asc()).all()
        result.append({
            "pile": pile_to_dict(pile),
            "current": _request_with_user(db, current) if current else None,
            "queued": [_request_with_user(db, r) for r in queued],
        })
    return result

@app.get("/api/admin/waiting-area")
def admin_waiting_area(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    waiting = db.query(ChargeRequest).filter(
        ChargeRequest.status == "waiting"
    ).order_by(ChargeRequest.waiting_start_time.asc()).all()
    fault = db.query(ChargeRequest).filter(
        ChargeRequest.status == "fault_waiting"
    ).order_by(ChargeRequest.waiting_start_time.asc()).all()
    return {
        "waiting": [_request_with_user(db, r) for r in waiting],
        "fault_waiting": [_request_with_user(db, r) for r in fault],
    }

@app.post("/api/admin/pile/start")
def admin_start_pile(payload: PileControlRequest, admin: User = Depends(require_admin),
                     db: Session = Depends(get_db)):
    pile = db.query(ChargingPile).filter(ChargingPile.id == payload.pile_id).first()
    if not pile:
        raise HTTPException(status_code=404, detail="充电桩不存在")
    return recover_pile(db, pile)

@app.post("/api/admin/pile/stop")
def admin_stop_pile(payload: FaultRequest, admin: User = Depends(require_admin),
                    db: Session = Depends(get_db)):
    pile = db.query(ChargingPile).filter(ChargingPile.id == payload.pile_id).first()
    if not pile:
        raise HTTPException(status_code=404, detail="充电桩不存在")
    return stop_fault_pile(db, pile, payload.strategy)

@app.put("/api/admin/pile/configure")
def set_parameters(payload: PileConfigRequest, admin: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    """设置充电桩参数（功率等），对应 SSD 3.5.3 setParameters"""
    pile = db.query(ChargingPile).filter(ChargingPile.id == payload.pile_id).first()
    if not pile:
        raise HTTPException(status_code=404, detail="充电桩不存在")
    if payload.power is not None:
        pile.power = payload.power
    db.add(pile)
    db.commit()
    db.refresh(pile)
    return {"message": f"{pile.code} 参数已更新", "pile": pile_to_dict(pile)}

@app.post("/api/admin/scheduler/call")
def admin_call_scheduler(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    run_scheduler(db)
    return {"message": "调度已执行"}

@app.get("/api/admin/fault/last")
def admin_last_fault(admin: User = Depends(require_admin)):
    return _last_fault_record or {"message": "暂无故障记录", "participants": [], "assignments": []}

@app.get("/api/admin/reports")
def admin_reports(period: str = Query(default="day", pattern="^(day|week|month)$"),
                  admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    run_scheduler(db)
    now = datetime.now()
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        label = start.strftime("%Y-%m-%d")
    elif period == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        label = f"{start.strftime('%Y-%m-%d')} ~ {(start + timedelta(days=6)).strftime('%Y-%m-%d')}"
    else:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        label = start.strftime("%Y-%m")

    bills = db.query(ChargeBill).filter(ChargeBill.generated_at >= start).all()
    rows = []
    for pile in db.query(ChargingPile).order_by(ChargingPile.id.asc()).all():
        pb = [b for b in bills if b.pile_id == pile.id]
        rows.append({
            "time_label": label, "period": period,
            "pile_id": pile.id, "pile_code": pile.code,
            "charge_count": len(pb),
            "total_duration": round(sum(b.charge_duration for b in pb), 4),
            "total_energy": round(sum(b.charge_energy for b in pb), 2),
            "electricity_fee": round(sum(b.electricity_fee for b in pb), 2),
            "service_fee": round(sum(b.service_fee for b in pb), 2),
            "total_fee": round(sum(b.total_fee for b in pb), 2),
        })
    return {"period": period, "rows": rows}

# ═══════════════════════════════════════════════════════════════
# 数据库初始化 & 种子数据
# ═══════════════════════════════════════════════════════════════

def seed_database() -> None:
    db = SessionLocal()
    try:
        # 管理员
        if not db.query(User).filter(User.username == "admin").first():
            db.add(User(username="admin", password_hash=hash_password("2"),
                       role="admin", nickname="管理员", battery_capacity=0))

        # 20 个测试用户
        capacities = [60, 70, 80, 55, 65, 90, 75, 62, 68, 72,
                      58, 64, 78, 82, 88, 66, 74, 92, 57, 69]
        for i, cap in enumerate(capacities, start=1):
            uname = f"{i:03d}"
            if not db.query(User).filter(User.username == uname).first():
                db.add(User(username=uname, password_hash=hash_password("1"),
                           role="user", nickname=f"测试车辆{i:03d}",
                           battery_capacity=cap))

        # 充电桩
        for i in range(FAST_PILE_COUNT):
            code = f"FastCharger-{i + 1}"
            if not db.query(ChargingPile).filter(ChargingPile.code == code).first():
                db.add(ChargingPile(code=code, type="fast", power=FAST_POWER, is_working=True))
        for i in range(SLOW_PILE_COUNT):
            code = f"SlowCharger-{i + 1}"
            if not db.query(ChargingPile).filter(ChargingPile.code == code).first():
                db.add(ChargingPile(code=code, type="slow", power=SLOW_POWER, is_working=True))

        db.commit()
    finally:
        db.close()

# 启动时初始化数据库（模块加载时执行）
Base.metadata.create_all(bind=engine)
seed_database()

@app.get("/")
def root():
    return {"name": "ChargeGo", "docs": "/docs", "status": "running"}

# ═══════════════════════════════════════════════════════════════
# 直接运行入口
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
