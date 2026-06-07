from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime

from app.db.database import Base


class AnalysisReport(Base):
    __tablename__ = "analysis_reports"

    id = Column(Integer, primary_key=True, index=True)
    repository_name = Column(String, index=True, nullable=False)
    analysis_data = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class AnalysisReportHistory(Base):
    __tablename__ = "analysis_report_history"

    id = Column(Integer, primary_key=True, index=True)
    repository_name = Column(String, index=True, nullable=False)
    analysis_data = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

