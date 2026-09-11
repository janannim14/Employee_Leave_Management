from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from database import supabase

app = FastAPI(title="Leave Management API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- PYDANTIC SCHEMAS ---
class EmployeeRegister(BaseModel):
    name: str
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str
    role: str  # "employee" or "manager"

class LeaveRequestCreate(BaseModel):
    employee_id: int
    start_date: str
    end_date: str
    reason: str

class LeaveStatusUpdate(BaseModel):
    status: str
    rejection_reason: Optional[str] = None

# --- AUTHENTICATION ENDPOINTS ---
@app.post("/register")
def register_employee(emp: EmployeeRegister):
    existing = supabase.table("employees").select("*").eq("email", emp.email).execute()
    if existing.data:
        raise HTTPException(status_code=400, detail="An account with this email already exists.")
    
    data = emp.model_dump()
    data["role"] = "employee"
    data["leave_balance"] = 20
    response = supabase.table("employees").insert(data).execute()
    return {"message": "Registration successful!", "user": response.data[0]}

@app.post("/login")
def login(credentials: LoginRequest):
    response = (
        supabase.table("employees")
        .select("*")
        .eq("email", credentials.email)
        .eq("password", credentials.password)
        .eq("role", credentials.role)
        .execute()
    )
    if not response.data:
        raise HTTPException(status_code=401, detail="Invalid Email, Password, or Role.")
    return {"message": "Login successful", "user": response.data[0]}

# --- EMPLOYEE ENDPOINTS ---
@app.post("/leave-requests/")
def create_leave_request(leave: LeaveRequestCreate):
    response = supabase.table("leave_requests").insert(leave.model_dump()).execute()
    return {"data": response.data}

@app.get("/leave-requests/employee/{emp_id}")
def get_employee_leaves(emp_id: int):
    # 1. Fetch all leave requests from Day 1 to present
    leaves_res = (
        supabase.table("leave_requests")
        .select("*")
        .eq("employee_id", emp_id)
        .order("id", desc=True)
        .execute()
    )
    
    # 2. Dynamically compute total approved leave days to fix balance discrepancies
    fmt = "%Y-%m-%d"
    total_approved_days = 0
    for req in leaves_res.data:
        if req.get("status", "").lower() == "approved":
            d1 = datetime.strptime(req["start_date"], fmt)
            d2 = datetime.strptime(req["end_date"], fmt)
            total_approved_days += (d2 - d1).days + 1

    # 3. Accurate remaining balance (calculated from total quota of 20 days)
    remaining_balance = max(0, 20 - total_approved_days)

    # 4. Keep employees table in sync
    supabase.table("employees").update({"leave_balance": remaining_balance}).eq("id", emp_id).execute()

    return {"leave_balance": remaining_balance, "leave_requests": leaves_res.data}

# --- MANAGER ENDPOINTS ---
@app.get("/leave-requests/manager")
def get_all_leave_requests_for_manager():
    # Fetch all leave requests alongside employee details
    response = (
        supabase.table("leave_requests")
        .select("*, employees(name, email, leave_balance)")
        .order("id", desc=True)
        .execute()
    )
    return {"leave_requests": response.data}

@app.patch("/leave-requests/{request_id}/status")
def update_leave_status(request_id: int, status_update: LeaveStatusUpdate):
    leave_res = supabase.table("leave_requests").select("*").eq("id", request_id).execute()
    if not leave_res.data:
        raise HTTPException(status_code=404, detail="Leave request not found")
    
    leave_data = leave_res.data[0]
    emp_id = leave_data["employee_id"]
    fmt = "%Y-%m-%d"

    # If manager is approving, ensure the employee has enough remaining balance
    if status_update.status.lower() == "approved" and leave_data["status"].lower() != "approved":
        emp_leaves = supabase.table("leave_requests").select("*").eq("employee_id", emp_id).execute()
        approved_days = 0
        for r in emp_leaves.data:
            if r.get("status", "").lower() == "approved":
                d1 = datetime.strptime(r["start_date"], fmt)
                d2 = datetime.strptime(r["end_date"], fmt)
                approved_days += (d2 - d1).days + 1
        
        current_balance = max(0, 20 - approved_days)

        req_d1 = datetime.strptime(leave_data["start_date"], fmt)
        req_d2 = datetime.strptime(leave_data["end_date"], fmt)
        requested_days = (req_d2 - req_d1).days + 1

        if current_balance < requested_days:
            raise HTTPException(
                status_code=400, 
                detail=f"Insufficient balance! Employee only has {current_balance} days remaining."
            )

    # Update status and rejection reason
    update_payload = {"status": status_update.status}
    if status_update.status.lower() == "rejected":
        update_payload["rejection_reason"] = status_update.rejection_reason or "No reason provided"
    else:
        update_payload["rejection_reason"] = None

    response = (
        supabase.table("leave_requests")
        .update(update_payload)
        .eq("id", request_id)
        .execute()
    )

    # Recalculate and update database balance record
    all_emp_leaves = supabase.table("leave_requests").select("*").eq("employee_id", emp_id).execute()
    total_approved = 0
    for r in all_emp_leaves.data:
        if r.get("status", "").lower() == "approved":
            d1 = datetime.strptime(r["start_date"], fmt)
            d2 = datetime.strptime(r["end_date"], fmt)
            total_approved += (d2 - d1).days + 1
            
    new_balance = max(0, 20 - total_approved)
    supabase.table("employees").update({"leave_balance": new_balance}).eq("id", emp_id).execute()

    return {"message": f"Leave request #{request_id} updated to {status_update.status}"}