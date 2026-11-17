from fastapi import APIRouter, Body, Request
from . import service

router = APIRouter(tags=["M7-资金覆盖对账"])


@router.get("/coverage")
def page(request: Request):
    data = service.read_ledger_demo(service.get_store(request))
    return request.app.state.templates.TemplateResponse(
        request, "m7_coverage.html", {"title": "资金覆盖对账", "data": data})


@router.post("/api/coverage/ledgers")
def open_ledger(request: Request, payload: dict = Body(default={})):
    """建账：分开记录项目获资承诺与来源占用。"""
    return service.open_ledger(service.get_store(request), payload or None)


@router.post("/api/coverage/revoke")
def revoke(request: Request, payload: dict = Body(...)):
    """撤销来源：释放原占用，重排未锁定占用做合规替补，不足记缺口。"""
    return service.revoke_source(service.get_store(request), payload)


@router.get("/api/coverage/ledgers/{ledger_id}")
def readback(request: Request, ledger_id: str):
    """回读每笔承诺的来源、缺口及撤销前后记录。"""
    return service.read_ledger(service.get_store(request), ledger_id)
