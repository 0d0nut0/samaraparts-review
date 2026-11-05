from sqlalchemy import Column, Integer, Numeric, DateTime, Text, Date
from app.database import Base

class AnalyticsSnapshot(Base):
    __tablename__ = "analytics_snapshots"

    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False, unique=True, index=True)
    total_sales = Column(Numeric(12, 2), default=0)
    total_profit = Column(Numeric(12, 2), default=0)
    top_products_json = Column(Text, nullable=True)

    def __repr__(self):
        return f"<AnalyticsSnapshot date={self.date} sales={self.total_sales}>"
