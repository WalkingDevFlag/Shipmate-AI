from fastapi import APIRouter, HTTPException, Query
from typing import List
from sqlalchemy.orm import Session
from fastapi import Depends
from app.db.database import get_db
from app.db.models import AnalysisReport, AnalysisReportHistory
from pydantic import BaseModel
from datetime import datetime

router = APIRouter()


class AnalysisReportHistoryResponse(BaseModel):
    id: int
    repository_name: str
    analysis_data: str
    created_at: datetime

    class Config:
        orm_mode = True


@router.get("/analysis/history", response_model=List[AnalysisReportHistoryResponse])
async def get_analysis_history(
    repository_name: str = Query(..., description="Repository name to fetch history for"),
    db: Session = Depends(get_db),
):
    """Retrieve historical analysis reports for a given repository."""
    history = (
        db.query(AnalysisReportHistory)
        .filter(AnalysisReportHistory.repository_name == repository_name)
        .order_by(AnalysisReportHistory.created_at.desc())
        .all()
    )
    if not history:
        raise HTTPException(status_code=404, detail="No analysis history found for the repository.")
    return history


@router.post("/analysis/history", status_code=201)
async def save_analysis_history(
    repository_name: str,
    analysis_data: str,
    db: Session = Depends(get_db),
):
    """Persist a new historical analysis report for a repository."""
    new_history = AnalysisReportHistory(
        repository_name=repository_name,
        analysis_data=analysis_data,
    )
    db.add(new_history)
    db.commit()
    db.refresh(new_history)
    return {"message": "Analysis history saved successfully.", "id": new_history.id}
