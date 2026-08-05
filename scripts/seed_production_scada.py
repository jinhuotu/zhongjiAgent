"""种子：隧道窑 / 配料楼 / 梭式窑烟气 —— 按通达画面量程生成模拟数据。

用法：
  poetry run python scripts/seed_production_scada.py
  poetry run python scripts/seed_production_scada.py --hours 48
"""

from __future__ import annotations

import argparse
import math
import random
import secrets
from datetime import datetime, timedelta

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from common.config import get_settings
from db.models.production import ProdAlarm, ProdCommand, ProdSample, ProdSystem, ProdTag


def _sync_url(url: str) -> str:
    return url.replace("mysql+asyncmy://", "mysql+pymysql://").replace(
        "mysql+aiomysql://", "mysql+pymysql://"
    )


def short_id(n: int = 10) -> str:
    return secrets.token_hex((n + 1) // 2)[:n]


def tag(
    system: str,
    code: str,
    name: str,
    unit: str | None = None,
    group: str | None = None,
    writable: bool = False,
    alarm_lo: float | None = None,
    alarm_hi: float | None = None,
    sort: int = 0,
) -> ProdTag:
    return ProdTag(
        system_code=system,
        tag_code=code,
        name=name,
        unit=unit,
        group_name=group,
        data_type="number",
        writable=writable,
        alarm_lo=alarm_lo,
        alarm_hi=alarm_hi,
        sort_order=sort,
        enabled=True,
    )


def tunnel_profile(pos: float) -> float:
    """沿窑长温度剖面（近似截图钟形曲线）。pos: 0~82"""
    # peak around pos 40
    x = (pos - 40) / 18
    peak = 1366 * math.exp(-(x * x))
    base = 50 + max(0, 200 - abs(pos - 40) * 3)
    return round(min(1500, max(40, peak + base * 0.15 + random.uniform(-8, 8))), 1)


def seed(hours: int = 24) -> None:
    settings = get_settings()
    engine = create_engine(_sync_url(settings.database_url), pool_pre_ping=True)
    now = datetime.now().replace(microsecond=0)
    step = timedelta(minutes=10)
    points = max(12, int(hours * 60 / 10))

    with Session(engine) as db:
        # systems
        systems = [
            ("tunnel", "隧道窑管理", "tunnel", "通达隧道窑控制系统（模拟数据）"),
            ("batching", "配料管理", "batching", "通达配料楼（模拟数据）"),
            ("shuttle", "梭式窑管理", "shuttle_flue", "梭式窑烟气治理系统（模拟数据）"),
        ]
        for code, name, kind, desc in systems:
            row = db.execute(select(ProdSystem).where(ProdSystem.code == code)).scalar_one_or_none()
            if row is None:
                db.add(
                    ProdSystem(
                        code=code,
                        name=name,
                        kind=kind,
                        description=desc,
                        enabled=True,
                        meta={"source": "seed", "screen": "tongda"},
                    )
                )
            else:
                row.name = name
                row.kind = kind
                row.description = desc

        # clear tags/samples/alarms/commands for reseed
        for code, *_ in systems:
            db.execute(delete(ProdSample).where(ProdSample.system_code == code))
            db.execute(delete(ProdAlarm).where(ProdAlarm.system_code == code))
            db.execute(delete(ProdCommand).where(ProdCommand.system_code == code))
            db.execute(delete(ProdTag).where(ProdTag.system_code == code))
        db.flush()

        # ---- tunnel tags ----
        tunnel_tags: list[ProdTag] = []
        zone_temps = {
            "Z2_TEMP": 1236,
            "Z3_TEMP": 1320,
            "Z4_TEMP": 1366,
            "Z5_TEMP": 1332,
            "Z6_TEMP": 1195,
        }
        for i, (code, base) in enumerate(zone_temps.items()):
            tunnel_tags.append(
                tag("tunnel", code, f"{code[:2]}区温度", "℃", "zone", False, 80, 1450, i)
            )
        tunnel_tags += [
            tag("tunnel", "AIR_TEMP", "助燃风温度", "℃", "utility", False, None, 350, 20),
            tag("tunnel", "KP1", "Kp1", "kPa", "pressure", False, -5, 5, 21),
            tag("tunnel", "KP2", "Kp2", "kPa", "pressure", False, -5, 5, 22),
            tag("tunnel", "R2", "R2", "℃", "pressure", False, None, None, 23),
            tag("tunnel", "CAR_NO", "窑车编号", None, "meta", False, sort=30),
            tag("tunnel", "CAR_POS", "车位", None, "meta", False, sort=31),
            tag("tunnel", "PLAN_TEMP", "计划温度", "℃", "meta", True, 800, 1450, 32),
            tag("tunnel", "QTY", "数量", None, "meta", False, sort=33),
        ]
        for z, gas, air in (
            ("Z2", 76.57, 607.3),
            ("Z3", 0.0, 296.7),
            ("Z4", 35.84, 406.9),
            ("Z5", 0.0, 376.2),
            ("Z6", 0.0, 12.5),
        ):
            tunnel_tags.append(tag("tunnel", f"{z}_GAS", f"{z}燃气", "m³/h", "flow", True, 0, 500, 40))
            tunnel_tags.append(tag("tunnel", f"{z}_AIR", f"{z}助燃风", "m³/h", "flow", True, 0, 800, 41))
        # profile points P00..P20 for chart
        for i in range(0, 21):
            pos = i * 4
            tunnel_tags.append(
                tag("tunnel", f"P{pos:02d}", f"剖面@{pos}", "℃", "profile", False, sort=100 + i)
            )
        db.add_all(tunnel_tags)

        # ---- batching tags ----
        batch_tags: list[ProdTag] = []
        active_silos = {1: 261, 3: 189, 8: 90, 11: 162, 13: 90}
        for i in range(1, 25):
            tgt = active_silos.get(i, 0)
            batch_tags.append(
                tag("batching", f"SILO{i:02d}_TGT", f"{i}#目标", "Kg", "silo", True, sort=i)
            )
            batch_tags.append(
                tag("batching", f"SILO{i:02d}_ACT", f"{i}#实际", "Kg", "silo", False, sort=i)
            )
            _ = tgt
        batch_tags += [
            tag("batching", "ADD_TGT", "外加料目标", "Kg", "silo", True, sort=50),
            tag("batching", "ADD_ACT", "外加料实际", "Kg", "silo", False, sort=51),
            tag("batching", "CART1_A", "秤车1-A", "Kg", "cart", False, sort=60),
            tag("batching", "CART1_B", "秤车1-B", "Kg", "cart", False, sort=61),
            tag("batching", "CART2_A", "秤车2-A", "Kg", "cart", False, sort=62),
            tag("batching", "CART2_B", "秤车2-B", "Kg", "cart", False, sort=63),
            tag("batching", "DISCHARGE1", "卸料口1", "Kg", "discharge", False, sort=70),
            tag("batching", "DISCHARGE2", "卸料口2", "Kg", "discharge", False, sort=71),
            tag("batching", "DISCHARGE3", "卸料口3", "Kg", "discharge", False, sort=72),
            tag("batching", "DISCHARGE4", "卸料口4", "Kg", "discharge", False, sort=73),
            tag("batching", "BATCH_NO", "锅次编号", None, "meta", False, sort=80),
            tag("batching", "RECIPE_NO", "配方编号", None, "meta", False, sort=81),
        ]
        db.add_all(batch_tags)

        # ---- shuttle flue tags ----
        shuttle_tags: list[ProdTag] = [
            tag("shuttle", "FIRE_TEMP", "当前烧成温度", "℃", "header", False, sort=1),
            tag("shuttle", "AIR_PRESS", "压缩空气压力", "MPa", "header", False, 0.05, 0.8, 2),
            tag("shuttle", "HX_A_TIN", "换热器A进口温", "℃", "hx", False, sort=10),
            tag("shuttle", "HX_A_TOUT", "换热器A出口温", "℃", "hx", False, sort=11),
            tag("shuttle", "HX_A_PIN", "换热器A进口压", "Pa", "hx", False, sort=12),
            tag("shuttle", "HX_B_TIN", "换热器B进口温", "℃", "hx", False, sort=13),
            tag("shuttle", "HX_B_TOUT", "换热器B出口温", "℃", "hx", False, sort=14),
            tag("shuttle", "COLD_A", "冷风阀A", "%", "valve", True, -100, 100, 20),
            tag("shuttle", "COLD_B", "冷风阀B", "%", "valve", True, -100, 100, 21),
            tag("shuttle", "FAN_HX_A_HZ", "换热风机A频率", "Hz", "fan", True, 0, 50, 30),
            tag("shuttle", "FAN_HX_A_A", "换热风机A电流", "A", "fan", False, sort=31),
            tag("shuttle", "FAN_HX_B_HZ", "换热风机B频率", "Hz", "fan", True, 0, 50, 32),
            tag("shuttle", "FAN_DRY_HZ", "烘干风机频率", "Hz", "fan", True, 0, 50, 33),
            tag("shuttle", "DRY_DP", "烘干除尘压差", "Pa", "dry", False, 0, 500, 34),
            tag("shuttle", "DENIT_TANK", "脱硝罐液位", "m³", "dosing", False, sort=40),
            tag("shuttle", "PUMP_A_HZ", "脱硝计量泵A", "Hz", "dosing", True, 0, 50, 41),
            tag("shuttle", "DES_TOWER_T", "脱硫塔温度", "℃", "tower", False, sort=50),
            tag("shuttle", "DES_TOWER_P", "脱硫塔压力", "Pa", "tower", False, -200, 50, 51),
            tag("shuttle", "SCR_T", "SCR温度", "℃", "tower", False, sort=52),
            tag("shuttle", "SCR_P", "SCR压力", "Pa", "tower", False, sort=53),
            tag("shuttle", "DUST_T", "除尘器温度", "℃", "tower", False, sort=54),
            tag("shuttle", "DUST_P", "除尘器压力", "Pa", "tower", False, sort=55),
            tag("shuttle", "IDF_HZ", "引风机频率", "Hz", "fan", True, 0, 50, 56),
            tag("shuttle", "SO2", "SO2实测", "mg/m³", "cems", False, None, 50, 60),
            tag("shuttle", "NOX", "NOx实测", "mg/m³", "cems", False, None, 100, 61),
            tag("shuttle", "DUST", "颗粒物实测", "mg/m³", "cems", False, None, 20, 62),
            tag("shuttle", "NH3", "NH3实测", "mg/m³", "cems", False, None, 8, 63),
            tag("shuttle", "O2", "烟气O2", "%", "cems", False, sort=64),
            tag("shuttle", "FG_TEMP", "烟气温度", "℃", "cems", False, sort=65),
            tag("shuttle", "FG_FLOW", "烟气流量", "m³/h", "cems", False, sort=66),
            tag("shuttle", "FG_VEL", "流速", "m/s", "cems", False, sort=67),
            tag("shuttle", "FG_HUM", "湿度", "%", "cems", False, sort=68),
        ]
        db.add_all(shuttle_tags)
        db.flush()

        def add_sample(sys: str, code: str, ts: datetime, num: float | None = None, text: str | None = None):
            db.add(
                ProdSample(
                    system_code=sys,
                    tag_code=code,
                    ts=ts,
                    value_num=num,
                    value_text=text,
                )
            )

        # generate history
        for i in range(points):
            ts = now - timedelta(minutes=10 * (points - 1 - i))
            wobble = math.sin(i / 7) * 0.02

            # tunnel
            for code, base in zone_temps.items():
                add_sample("tunnel", code, ts, round(base * (1 + wobble) + random.uniform(-5, 5), 1))
            add_sample("tunnel", "AIR_TEMP", ts, round(183 + random.uniform(-3, 3), 1))
            add_sample("tunnel", "KP1", ts, round(-1.48 + random.uniform(-0.2, 0.2), 2))
            add_sample("tunnel", "KP2", ts, round(2.70 + random.uniform(-0.2, 0.2), 2))
            add_sample("tunnel", "R2", ts, round(1159 + random.uniform(-10, 10), 1))
            add_sample("tunnel", "CAR_NO", ts, 37)
            add_sample("tunnel", "CAR_POS", ts, 40)
            add_sample("tunnel", "PLAN_TEMP", ts, 1365)
            add_sample("tunnel", "QTY", ts, 366)
            for z, gas, air in (
                ("Z2", 76.57, 607.3),
                ("Z3", 0.0, 296.7),
                ("Z4", 35.84, 406.9),
                ("Z5", 0.0, 376.2),
                ("Z6", 0.0, 12.5),
            ):
                add_sample("tunnel", f"{z}_GAS", ts, round(max(0, gas * (1 + wobble)), 2))
                add_sample("tunnel", f"{z}_AIR", ts, round(max(0, air * (1 + wobble * 0.5)), 1))
            for p in range(0, 21):
                pos = p * 4
                add_sample("tunnel", f"P{pos:02d}", ts, tunnel_profile(pos))

            # batching — keep stable recipe-like values, slight noise on act
            for silo, tgt in active_silos.items():
                add_sample("batching", f"SILO{silo:02d}_TGT", ts, float(tgt))
                add_sample(
                    "batching",
                    f"SILO{silo:02d}_ACT",
                    ts,
                    round(tgt + random.uniform(-2, 2.5), 1),
                )
            for silo in range(1, 25):
                if silo in active_silos:
                    continue
                add_sample("batching", f"SILO{silo:02d}_TGT", ts, 0.0)
                add_sample("batching", f"SILO{silo:02d}_ACT", ts, 0.0)
            add_sample("batching", "ADD_TGT", ts, 0.0)
            add_sample("batching", "ADD_ACT", ts, 0.0)
            add_sample("batching", "CART1_A", ts, 585.0)
            add_sample("batching", "CART1_B", ts, 316.5)
            add_sample("batching", "CART2_A", ts, 0.0)
            add_sample("batching", "CART2_B", ts, 0.0)
            add_sample("batching", "DISCHARGE1", ts, 0.0)
            add_sample("batching", "DISCHARGE2", ts, 901.5)
            add_sample("batching", "DISCHARGE3", ts, 0.0)
            add_sample("batching", "DISCHARGE4", ts, 0.0)
            add_sample("batching", "BATCH_NO", ts, text="202605161702")
            add_sample("batching", "RECIPE_NO", ts, text="20260508141020")

            # shuttle
            add_sample("shuttle", "FIRE_TEMP", ts, round(max(0, 20 + i * 0.1), 1))
            add_sample("shuttle", "AIR_PRESS", ts, 0.1)
            add_sample("shuttle", "HX_A_TIN", ts, 24)
            add_sample("shuttle", "HX_A_TOUT", ts, 31)
            add_sample("shuttle", "HX_A_PIN", ts, -9)
            add_sample("shuttle", "HX_B_TIN", ts, 24)
            add_sample("shuttle", "HX_B_TOUT", ts, 28)
            add_sample("shuttle", "COLD_A", ts, -25.0)
            add_sample("shuttle", "COLD_B", ts, -25.0)
            add_sample("shuttle", "FAN_HX_A_HZ", ts, 0.0)
            add_sample("shuttle", "FAN_HX_A_A", ts, 0.0)
            add_sample("shuttle", "FAN_HX_B_HZ", ts, 0.0)
            add_sample("shuttle", "FAN_DRY_HZ", ts, 35.6)
            add_sample("shuttle", "DRY_DP", ts, 104)
            add_sample("shuttle", "DENIT_TANK", ts, 0.306)
            add_sample("shuttle", "PUMP_A_HZ", ts, 0.0)
            add_sample("shuttle", "DES_TOWER_T", ts, 27)
            add_sample("shuttle", "DES_TOWER_P", ts, -87.3)
            add_sample("shuttle", "SCR_T", ts, 0)
            add_sample("shuttle", "SCR_P", ts, 0.0)
            add_sample("shuttle", "DUST_T", ts, 27)
            add_sample("shuttle", "DUST_P", ts, 16.7)
            add_sample("shuttle", "IDF_HZ", ts, 0.0)
            add_sample("shuttle", "SO2", ts, round(8 + random.uniform(0, 4), 2))
            add_sample("shuttle", "NOX", ts, round(25 + random.uniform(0, 10), 2))
            add_sample("shuttle", "DUST", ts, round(2 + random.uniform(0, 1.5), 2))
            add_sample("shuttle", "NH3", ts, round(0.5 + random.uniform(0, 0.8), 2))
            add_sample("shuttle", "O2", ts, 20.8)
            add_sample("shuttle", "FG_TEMP", ts, 26)
            add_sample("shuttle", "FG_FLOW", ts, 1021.32)
            add_sample("shuttle", "FG_VEL", ts, 0.29)
            add_sample("shuttle", "FG_HUM", ts, 1.71)

        # sample alarms
        db.add_all(
            [
                ProdAlarm(
                    public_id=short_id(12),
                    system_code="tunnel",
                    tag_code="Z4_TEMP",
                    level="warning",
                    title="四区温度接近计划上限",
                    message="Z4 实测接近计划温度 1365℃，请关注保温段稳定性",
                    status="active",
                    raised_at=now - timedelta(minutes=35),
                ),
                ProdAlarm(
                    public_id=short_id(12),
                    system_code="batching",
                    tag_code="SILO01_ACT",
                    level="info",
                    title="1#仓实际略超目标",
                    message="目标 261Kg，实际约 262.5Kg",
                    status="acked",
                    raised_at=now - timedelta(hours=2),
                    acked_at=now - timedelta(hours=1),
                ),
                ProdAlarm(
                    public_id=short_id(12),
                    system_code="shuttle",
                    tag_code="IDF_HZ",
                    level="warning",
                    title="引风机频率为 0",
                    message="引风机当前停机或未联机，烟气治理处于待机状态",
                    status="active",
                    raised_at=now - timedelta(minutes=12),
                ),
                ProdAlarm(
                    public_id=short_id(12),
                    system_code="shuttle",
                    tag_code="DES_TOWER_P",
                    level="alarm",
                    title="脱硫塔负压偏高",
                    message="脱硫塔压力 -87.3 Pa，请检查引风与阀门",
                    status="active",
                    raised_at=now - timedelta(minutes=8),
                ),
            ]
        )

        db.commit()
        print(f"seeded production scada: hours={hours}, points={points}, systems={len(systems)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()
    seed(hours=args.hours)
