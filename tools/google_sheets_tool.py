# tools/google_sheets_tool.py
import gspread
import os
import base64
import json
from datetime import datetime, date, timedelta
from crewai.tools import tool
import calendar

# --- 辅助函数 ---

def _get_creds():
    """
    获取Google服务账号凭证。
    优先从环境变量 GOOGLE_CREDENTIALS_BASE64 读取（用于生产环境，如Render）。
    如果环境变量不存在，则回退到从本地JSON文件加载（用于本地开发）。
    """
    # Production: Load from environment variable
    creds_base64 = os.getenv("GOOGLE_CREDENTIALS_BASE64")
    if creds_base64:
        print("[LOG] Found GOOGLE_CREDENTIALS_BASE64 env var. Loading creds from environment.")
        creds_json_str = base64.b64decode(creds_base64).decode('utf-8')
        creds_json = json.loads(creds_json_str)
        return gspread.service_account_from_dict(creds_json)
    
    # Local Development: Load from file path
    else:
        local_creds_path = "/home/shigobo/gdrive_service_account.json"
        print(f"[LOG] GOOGLE_CREDENTIALS_BASE64 not found. Falling back to local path: {local_creds_path}")
        if not os.path.exists(local_creds_path):
            # Use ASCII-only characters for the exception message
            raise FileNotFoundError(f"Local credentials file not found. Please ensure it is located at: {local_creds_path}")
        return gspread.service_account(filename=local_creds_path)

def _get_worksheet():
    """获取Google Sheet的第一个工作表对象。"""
    gc = _get_creds()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise ValueError("SPREADSHEET_ID environment variable not set.")
    spreadsheet = gc.open_by_key(spreadsheet_id)
    return spreadsheet.sheet1

def _find_or_create_row(worksheet, target_date_str: str):
    """
    在表格中查找指定日期的行。如果找不到，则在保持日期顺序的情况下创建新行。
    返回该行的索引（行号）。
    """
    try:
        cell = worksheet.find(target_date_str, in_column=1)
        return cell.row
    except gspread.CellNotFound:
        target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
        all_dates_str = worksheet.col_values(1)
        
        insert_row_index = len(all_dates_str) + 1
        for i, current_date_str in enumerate(all_dates_str[1:], start=2):
            try:
                current_date = datetime.strptime(current_date_str, "%Y-%m-%d").date()
                if target_date < current_date:
                    insert_row_index = i
                    break
            except ValueError:
                continue
        
        weekday_str = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][target_date.weekday()]
        workday_status = "是" if target_date.weekday() < 5 else "否"
        new_row_data = [target_date_str, weekday_str, workday_status, "18:00:00"]
        
        worksheet.insert_row(new_row_data, index=insert_row_index, value_input_option='USER_ENTERED')
        return insert_row_index

# --- CrewAI 工具 ---

@tool("Update Schedule Tool")
def update_schedule_tool(target_date: str, is_workday: bool) -> str:
    """
    Updates the workday status for a specific date.
    Args:
        target_date (str): The target date in 'YYYY-MM-DD' format.
        is_workday (bool): True if it's a workday, False if it's a day off.
    """
    try:
        worksheet = _get_worksheet()
        row_index = _find_or_create_row(worksheet, target_date)
        workday_status = "是" if is_workday else "否"
        worksheet.update_cell(row_index, 3, workday_status)
        return f"成功将日期 {target_date} 的状态更新为 {'工作日' if is_workday else '休息日'}。"
    except Exception as e:
        import traceback
        return f"Error updating schedule: {type(e).__name__} - {e}\n{traceback.format_exc()}"

@tool("Clock Out and Calculate Tool")
def clock_out_tool() -> str:
    """
    Records today's clock-out time and calculates overtime. Optimized for performance.
    """
    try:
        worksheet = _get_worksheet()
        today = date.today()
        today_str = today.strftime("%Y-%m-%d")
        
        print(f"[LOG] Clocking out for date: {today_str}")
        row_index = _find_or_create_row(worksheet, today_str)
        
        cell_range = f"C{row_index}:D{row_index}"
        cell_values = worksheet.get(cell_range, value_render_option='FORMATTED_VALUE')
        
        is_workday = cell_values[0][0] if len(cell_values) > 0 and len(cell_values[0]) > 0 else ''
        standard_off_time_str = cell_values[0][1] if len(cell_values) > 0 and len(cell_values[0]) > 1 else '18:00:00'

        if not is_workday or is_workday.lower() != '是':
            return f"根据你的计划，今天 ({today_str}) 不是工作日，无需记录下班。"

        now = datetime.now()
        off_work_time_str = now.strftime("%H:%M:%S")
        standard_off_time = datetime.strptime(standard_off_time_str, "%H:%M:%S").time()
        actual_off_time = now.time()
        overtime_delta = datetime.combine(today, actual_off_time) - datetime.combine(today, standard_off_time)
        overtime_hours = max(0, overtime_delta.total_seconds() / 3600)

        all_values = worksheet.get_all_values()
        monthly_total_overtime = 0
        if len(all_values) > 1:
            data_rows = all_values[1:]
            current_month = today.month
            current_year = today.year
            current_day_overtime = overtime_hours

            for i, row in enumerate(data_rows):
                try:
                    if i + 2 == row_index:
                        continue
                    if len(row) > 5:
                        date_str, overtime_str = row[0], row[5]
                        if not date_str or not overtime_str:
                            continue
                        row_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                        if row_date.year == current_year and row_date.month == current_month:
                            monthly_total_overtime += float(overtime_str)
                except (ValueError, TypeError):
                    continue
            monthly_total_overtime += current_day_overtime

        update_requests = [
            {'range': f'E{row_index}', 'values': [[off_work_time_str]]},
            {'range': f'F{row_index}', 'values': [[f"{overtime_hours:.2f}"]]},
            {'range': f'G{row_index}', 'values': [[f"{monthly_total_overtime:.2f}"]]},
        ]
        worksheet.batch_update(update_requests, value_input_option='USER_ENTERED')

        future_workdays = 0
        today_sheet_index = row_index - 2
        if today_sheet_index != -1:
            for i in range(today_sheet_index + 1, len(all_values) - 1):
                if len(all_values[i+1]) > 2 and str(all_values[i+1][2]).strip().lower() == '是':
                    future_workdays += 1

        remaining_overtime_budget = 29 - monthly_total_overtime
        suggestion = ""
        if remaining_overtime_budget <= 0:
            suggestion = f"警告！你本月的加班时长已达 {monthly_total_overtime:.2f} 小时。接下来请务必准时下班！"
        elif future_workdays > 0:
            avg_overtime_per_day = remaining_overtime_budget / future_workdays
            suggested_off_time_seconds = 18 * 3600 + avg_overtime_per_day * 3600
            h = int(suggested_off_time_seconds // 3600)
            m = int((suggested_off_time_seconds % 3600) // 60)
            suggestion = f"本月还剩 {future_workdays} 个工作日。为达标，接下来建议在 {h:02d}:{m:02d} 左右下班。"
        else:
            suggestion = "本月已无剩余工作日，请好好休息！"

        return (f"成功记录下班时间：{off_work_time_str}。\n"
                f"当日加班：{overtime_hours:.2f} 小时。\n"
                f"本月累计加班：{monthly_total_overtime:.2f} 小时。\n\n"
                f"【智能建议】\n{suggestion}")

    except Exception as e:
        import traceback
        return f"Error during clock-out: {type(e).__name__} - {e}\n{traceback.format_exc()}"

@tool("Populate Month Schedule Tool")
def populate_month_schedule_tool(year: int, month: int) -> str:
    """
    Populates a month with default workdays (Mon-Fri) and weekends.
    """
    try:
        worksheet = _get_worksheet()
        print(f"[LOG] Populating schedule for {year}-{month}")
        
        existing_dates = set(worksheet.col_values(1))
        
        num_days = calendar.monthrange(year, month)[1]
        new_rows = []
        
        for day in range(1, num_days + 1):
            target_date = date(year, month, day)
            target_date_str = target_date.strftime("%Y-%m-%d")

            if target_date_str in existing_dates:
                continue

            weekday_str = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][target_date.weekday()]
            is_workday = target_date.weekday() < 5 
            workday_status = "是" if is_workday else "否"
            
            new_row_data = [target_date_str, weekday_str, workday_status, "18:00:00"]
            new_rows.append(new_row_data)

        if not new_rows:
            return f"{year}年{month}月的排班已存在，无需填充。"
            
        worksheet.append_rows(new_rows, value_input_option='USER_ENTERED')
        
        return f"成功为 {year}年{month}月 填充了 {len(new_rows)} 天的默认排班！您现在可以进行个性化调整。"
        
    except Exception as e:
        import traceback
        return f"Error populating month schedule: {type(e).__name__} - {e}\n{traceback.format_exc()}"

@tool("Get Daily Suggestion Tool")
def get_daily_suggestion_tool() -> str:
    """
    获取今天的下班建议。它只读取数据进行计算，不写入任何内容。
    """
    print("--- [LOG] Entering get_daily_suggestion_tool ---")
    try:
        today = date.today()
        print(f"[LOG] Today's date: {today}")

        if today.weekday() >= 5:
            suggestion = "今天是周末，好好休息吧！"
            print(f"[LOG] Today is a weekend. Returning: {suggestion}")
            return suggestion

        print("[LOG] Connecting to worksheet...")
        worksheet = _get_worksheet()
        print("[LOG] Successfully connected to worksheet.")
        
        today_str = today.strftime("%Y-%m-%d")
        print(f"[LOG] Formatted today's date string: {today_str}")

        try:
            cell = worksheet.find(today_str, in_column=1)
            if not cell:
                return "今天的计划尚未设定，请先规划日程。"
            
            is_workday_value = worksheet.cell(cell.row, 3).value
            if not is_workday_value or str(is_workday_value).strip().lower() != '是':
                return "根据计划，今天不是工作日，好好休息！"

        except gspread.CellNotFound:
            return "今天的计划尚未设定，请先规划日程。 (CellNotFound)"
        except Exception as e:
            return f"Error while checking for workday: {e}"

        print("[LOG] Proceeding to overtime calculation for the current month...")
        
        all_values = worksheet.get_all_values()
        if not all_values or len(all_values) < 2:
            monthly_total_overtime = 0
        else:
            data_rows = all_values[1:]
            current_month = today.month
            current_year = today.year
            monthly_total_overtime = 0
            date_col_index = 0
            overtime_col_index = 5

            for row in data_rows:
                try:
                    if len(row) > max(date_col_index, overtime_col_index):
                        date_str = row[date_col_index]
                        overtime_str = row[overtime_col_index]
                        if not date_str or not overtime_str:
                            continue
                        row_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                        if row_date.year == current_year and row_date.month == current_month:
                            monthly_total_overtime += float(overtime_str)
                except (ValueError, TypeError):
                    print(f"[LOG] Skipping row due to parsing error: {row}")
                    continue
        
        print(f"[LOG] Calculated monthly_total_overtime for {current_year}-{current_month}: {monthly_total_overtime}")

        all_dates = [row[0] for row in all_values]
        all_workday_flags = [row[2] for row in all_values]
        
        future_workdays = 0
        today_sheet_index = -1
        if today_str in all_dates:
            today_sheet_index = all_dates.index(today_str)

        if today_sheet_index != -1:
            for i in range(today_sheet_index + 1, len(all_dates)):
                try:
                    future_date = datetime.strptime(all_dates[i], "%Y-%m-%d").date()
                    if future_date.month != current_month:
                        continue
                except (ValueError, IndexError):
                    continue

                if i < len(all_workday_flags) and str(all_workday_flags[i]).strip().lower() == '是':
                    future_workdays += 1
        
        remaining_overtime_budget = 29 - monthly_total_overtime
        total_remaining_workdays = future_workdays + 1
        
        suggestion = ""
        if remaining_overtime_budget <= 0:
            suggestion = f"加班额度已用完 (剩余 {remaining_overtime_budget:.2f} 小时)，请18:00准时下班！"
        elif total_remaining_workdays > 0:
            avg_overtime_per_day = remaining_overtime_budget / total_remaining_workdays
            suggested_off_time_seconds = 18 * 3600 + avg_overtime_per_day * 3600
            h = int(suggested_off_time_seconds // 3600)
            m = int((suggested_off_time_seconds % 3600) // 60)
            suggestion = f"若要均分剩余加班，今天建议在 {h:02d}:{m:02d} 下班。"
        else:
            suggestion = "请18:00准时下班，享受生活！"
        
        return suggestion

    except Exception as e:
        import traceback
        error_message = f"Fatal error in get_daily_suggestion_tool: {type(e).__name__} - {e}\n{traceback.format_exc()}"
        print(f"[LOG] --- FATAL ERROR --- \n{error_message}")
        return error_message