from enum import Enum


class LeadStatus(str, Enum):
    NEW = "new"
    CONTACTED = "contacted"
    CONSULTING = "consulting"
    BOOKED = "booked"
    VISITED = "visited"
    SOLD = "sold"
    LOST = "lost"