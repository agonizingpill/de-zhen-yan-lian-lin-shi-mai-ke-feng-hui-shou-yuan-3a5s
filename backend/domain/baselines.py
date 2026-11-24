from datetime import datetime,timezone

from fastapi import HTTPException
from sqlalchemy import Boolean,CheckConstraint,Column,DateTime,ForeignKey,Integer,JSON,String,Table,select

from core.audit import record
from core.db import metadata,transaction
from domain.catalog import one,query
from domain.interventions import findings
from domain.learning import lof_score
from domain.soundboard import points,windows
from domain.states import Baseline,Finding,SCORE_LIMIT

versions = Table(
    "baseline_versions",metadata,
    Column("code",String(24),primary_key=True),
    Column("state",String(10),nullable=False),
    Column("windows",JSON,nullable=False),
    Column("created_by",String(80),nullable=False),
    Column("created_at",DateTime,nullable=False),
    Column("activated_at",DateTime),
    Column("retired_at",DateTime),
    Column("predecessor",String(24)),
    CheckConstraint("state IN ('candidate','on_duty','retired')"),
)

comparisons = Table(
    "baseline_comparisons",metadata,
    Column("candidate",String(24),ForeignKey("baseline_versions.code"),primary_key=True),
    Column("against",String(24),ForeignKey("baseline_versions.code"),nullable=False),
    Column("samples",JSON,nullable=False),
    Column("passed",Boolean,nullable=False),
    Column("computed_at",DateTime,nullable=False),
)

baseline_mutex = Table("baseline_serial",metadata,Column("code",Integer,primary_key=True))


def now():
    return datetime.now(timezone.utc)


def lock_baselines(conn):
    conn.execute(select(baseline_mutex).with_for_update()).first()


def on_duty(conn):
    row=conn.execute(select(versions).where(versions.c.state==Baseline.ON_DUTY)).mappings().first()
    if row is None:
        raise HTTPException(409,"没有当班基线，请先启用一套候选基线")
    return dict(row)


def version_windows(conn,version):
    codes=list(version["windows"])
    rows=[dict(row) for row in conn.execute(select(windows).where(windows.c.code.in_(codes))).mappings()]
    if len(rows)!=len(codes):
        raise HTTPException(409,f"基线 {version['code']} 的声检窗不完整")
    return rows


def confirmed(conn):
    stmt=(select(windows,findings.c.code.label("finding"),findings.c.category.label("category"))
          .join(findings,findings.c.focus_window==windows.c.code))
    samples=[]
    for row in conn.execute(stmt).mappings():
        sample=dict(row)
        sample["label"]="normal" if sample["category"]==Finding.NORMAL else "abnormal"
        samples.append(sample)
    return samples


def evaluate(base_rows,samples):
    report=[]
    for sample in samples:
        score,_=lof_score(base_rows,sample)
        side="normal" if score>=SCORE_LIMIT else "abnormal"
        report.append({"window":sample["code"],"finding":sample["finding"],"label":sample["label"],
                       "score":score,"ok":side==sample["label"]})
    return report


def compare(conn,candidate,duty):
    samples=confirmed(conn)
    own={entry["window"]:entry for entry in evaluate(version_windows(conn,candidate),samples)}
    ref={entry["window"]:entry for entry in evaluate(version_windows(conn,duty),samples)}
    entries=[{"window":sample["code"],"finding":sample["finding"],"label":sample["label"],
              "duty_score":ref[sample["code"]]["score"],"duty_ok":ref[sample["code"]]["ok"],
              "candidate_score":own[sample["code"]]["score"],"candidate_ok":own[sample["code"]]["ok"]}
             for sample in samples]
    return {"candidate":candidate["code"],"against":duty["code"],"samples":entries,
            "passed":bool(entries) and all(entry["candidate_ok"] for entry in entries),
            "computed_at":now()}


def store_comparison(conn,comparison):
    conn.execute(comparisons.delete().where(comparisons.c.candidate==comparison["candidate"]))
    conn.execute(comparisons.insert().values(**comparison))


def refresh(conn,duty=None):
    duty=duty or on_duty(conn)
    for row in conn.execute(select(versions).where(versions.c.state==Baseline.CANDIDATE)).mappings():
        store_comparison(conn,compare(conn,dict(row),duty))


def create(form,user):
    with transaction() as conn:
        if conn.execute(select(versions.c.code).where(versions.c.code==form.code)).first():
            raise HTTPException(409,"基线版本编号已存在")
        codes=[item.code for item in form.windows]
        if len(set(codes))!=len(codes):
            raise HTTPException(409,"候选基线内的声检窗编号重复")
        if conn.execute(select(windows.c.code).where(windows.c.code.in_(codes))).first():
            raise HTTPException(409,"候选基线声检窗编号与既有窗口冲突")
        for item in form.windows:
            point=one(conn,points,item.point)
            taken=conn.execute(select(windows.c.code).where(windows.c.point==item.point,
                windows.c.slot==item.slot,windows.c.revision==point["revision"])).first()
            if taken:
                raise HTTPException(409,f"麦位 {item.point} 在本接线版本的时刻 {item.slot} 已被占用")
            conn.execute(windows.insert().values(**item.model_dump(),channel=point["channel"],
                         revision=point["revision"],purpose="baseline"))
        conn.execute(versions.insert().values(code=form.code,state=Baseline.CANDIDATE,windows=codes,
                                              created_by=user["sub"],created_at=now()))
        store_comparison(conn,compare(conn,one(conn,versions,form.code),on_duty(conn)))
        record(conn,user,f"baseline:candidate:{form.code}")
        return one(conn,versions,form.code)


def enable(code,form,user):
    with transaction() as conn:
        lock_baselines(conn)
        candidate=one(conn,versions,code,lock=True)
        if candidate["state"]!=Baseline.CANDIDATE:
            raise HTTPException(409,"仅候选基线可以启用")
        duty=on_duty(conn)
        if form.expected and form.expected!=duty["code"]:
            raise HTTPException(409,f"当班基线已切换为 {duty['code']}，候选比对已按新当班重算，请核对后再启用")
        samples=confirmed(conn)
        labels={sample["label"] for sample in samples}
        if "normal" not in labels or "abnormal" not in labels:
            raise HTTPException(409,"正常和异常样本齐了才可启用候选基线")
        misses=[entry for entry in evaluate(version_windows(conn,candidate),samples) if not entry["ok"]]
        if misses:
            raise HTTPException(409,"候选基线未通过已确认样本比对（"+
                                "、".join(entry["window"] for entry in misses)+"），保留当班基线")
        stamp=now()
        conn.execute(versions.update().where(versions.c.code==duty["code"])
                     .values(state=Baseline.RETIRED,retired_at=stamp))
        conn.execute(versions.update().where(versions.c.code==candidate["code"])
                     .values(state=Baseline.ON_DUTY,activated_at=stamp,retired_at=None,
                             predecessor=duty["code"]))
        refresh(conn,one(conn,versions,candidate["code"]))
        record(conn,user,f"baseline:enable:{code}")
        return one(conn,versions,code)


def rollback(user):
    with transaction() as conn:
        lock_baselines(conn)
        duty=on_duty(conn)
        if not duty["predecessor"]:
            raise HTTPException(409,"没有可回退的上一版基线")
        previous=one(conn,versions,duty["predecessor"])
        stamp=now()
        conn.execute(versions.update().where(versions.c.code==duty["code"])
                     .values(state=Baseline.RETIRED,retired_at=stamp))
        conn.execute(versions.update().where(versions.c.code==previous["code"])
                     .values(state=Baseline.ON_DUTY,activated_at=stamp,retired_at=None,
                             predecessor=duty["code"]))
        refresh(conn,one(conn,versions,previous["code"]))
        record(conn,user,f"baseline:rollback:{previous['code']}")
        return one(conn,versions,previous["code"])


def board():
    with transaction() as conn:
        duty=on_duty(conn)
        detailed=[]
        for version in query(conn,versions):
            detailed.append({**version,"windows":version_windows(conn,version)})
        samples=confirmed(conn)
        normal=sum(1 for sample in samples if sample["label"]=="normal")
        compared=[dict(row) for row in
                  conn.execute(select(comparisons).order_by(comparisons.c.candidate)).mappings()]
        return {"on_duty":duty["code"],"versions":detailed,"comparisons":compared,
                "confirmed":{"normal":normal,"abnormal":len(samples)-normal,"total":len(samples)}}
