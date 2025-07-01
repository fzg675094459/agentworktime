# tools/google_sheets_tool.py
import gspread
import os
import base64
import json
from datetime import datetime, date, timedelta
from crewai.tools import tool

# --- 辅助函数 ---

def _get_creds():
    """从环境变量中获取并解码Google服务账号凭证。"""
    creds_base64 = os.getenv("GOOGLE_CREDENTIALS_BASE64")
    if not creds_base64:
        raise ValueError("GOOGLE_CREDENTIALS_BASE64 environment variable not set.")
    creds_json_str = base64.b64decode(creds_base64).decode('utf-8')
    creds_json = json.loads(creds_json_str)
    return gspread.service_account_from_dict(creds_json)

def _get_worksheet():
    """获取Google Sheet的第一个工作表对象。"""
    gc = _get_creds()
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise ValueError("SPREADSHEET_ID environment variable not set.")
    spreadsheet = gc.open_by_key(spreadsheet_id)
    return spreadsheet.sheet1

# --- 这是最终的、最可靠的实现 ---
def _find_or_create_row(worksheet, target_date_str: str):
    """
    在表格中查找指定日期的行。如果找不到，则在保持日期顺序的情况下创建新行。
    返回该行的索引（行号）。
    """
    # 尝试查找单元格
    cell = worksheet.find(target_date_str, in_column=1)
    
    # 检查查找结果
    if cell:
        # 如果找到了，直接返回行号
        return cell.row
    else:
        # 如果没找到，则创建新行并插入到正确的位置
        target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
        
        # 获取第一列所有日期
        all_dates_str = worksheet.col_values(1)
        
        # 寻找正确的插入位置，跳过表头
        insert_row_index = len(all_dates_str) + 1 # 默认为在末尾追加
        for i, current_date_str in enumerate(all_dates_str[1:], start=2): # gspread行号从1开始, 我们跳过第1行表头
            try:
                current_date = datetime.strptime(current_date_str, "%Y-%m-%d").date()
                if target_date < current_date:
                    insert_row_index = i
                    break
            except ValueError:
                # 忽略无法解析为日期的行
                continue
        
        # 准备新行数据
        weekday_str = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][target_date.weekday()]
        # 默认根据是否为周末来判断工作状态
        workday_status = "是" if target_date.weekday() < 5 else "否"
        new_row_data = [target_date_str, weekday_str, workday_status, "18:00:00"]
        
        # 在计算出的位置插入新行
        worksheet.insert_row(new_row_data, index=insert_row_index, value_input_option='USER_ENTERED')
        
        # 返回新行的行号
        return insert_row_index

# --- 新工具 1: 更新排班 ---
@tool("Update Schedule Tool")
def update_schedule_tool(target_date: str, is_workday: bool) -> str:
    """
    更新指定日期的工作状态。
    Args:
        target_date (str): 目标日期，格式必须是 'YYYY-MM-DD'。
        is_workday (bool): True表示当天是工作日，False表示当天是休息日。
    """
    try:
        worksheet = _get_worksheet()
        row_index = _find_or_create_row(worksheet, target_date)
        workday_status = "是" if is_workday else "否"
        
        worksheet.update_cell(row_index, 3, workday_status)
        
        return f"成功将日期 {target_date} 的状态更新为 {'工作日' if is_workday else '休息日'}。"
    except Exception as e:
        import traceback
        return f"更新排班时发生错误: {type(e).__name__} - {e}\n{traceback.format_exc()}"

# --- 新工具 2: 记录和计算加班 ---
@tool("Clock Out and Calculate Tool")
def clock_out_tool() -> str:
    """
    记录今天的下班时间，并计算加班时长。仅在用户点击“我下班啦”时使用。
    此版本经过优化，使用 batch_update 来减少 API 调用，提高响应速度。
    """
    try:
        worksheet = _get_worksheet()
        today = date.today()
        today_str = today.strftime("%Y-%m-%d")
        
        # --- 1. 查找或创建今天的行，并获取所需数据 ---
        # 为了减少API调用，我们一次性获取多一点数据
        row_index = _find_or_create_row(worksheet, today_str)
        # 使用 batch_get 获取多个单元格，比单个cell()调用更高效
        cell_range = f"C{row_index}:D{row_index}"
        cell_values = worksheet.get(cell_range, value_render_option='FORMATTED_VALUE')
        
        is_workday = cell_values[0][0] if len(cell_values) > 0 and len(cell_values[0]) > 0 else ''
        standard_off_time_str = cell_values[0][1] if len(cell_values) > 0 and len(cell_values[0]) > 1 else '18:00:00'

        if not is_workday or is_workday.lower() != '是':
            return f"根据你的计划，今天 ({today_str}) 不是工作日，无需记录下班。"

        # --- 2. 计算加班数据 ---
        now = datetime.now()
        off_work_time_str = now.strftime("%H:%M:%S")
        
        standard_off_time = datetime.strptime(standard_off_time_str, "%H:%M:%S").time()
        actual_off_time = now.time()
        
        overtime_delta = datetime.combine(today, actual_off_time) - datetime.combine(today, standard_off_time)
        overtime_hours = max(0, overtime_delta.total_seconds() / 3600)

        # --- 3. 计算月度累计加班 (与 get_suggestion_tool 逻辑保持一致) ---
        all_values = worksheet.get_all_values()
        monthly_total_overtime = 0
        if len(all_values) > 1:
            data_rows = all_values[1:]
            current_month = today.month
            current_year = today.year
            
            # 在累加前，先把当前计算出的加班时间加上
            # 这样可以避免再次读取表格，并确保数据是完全最新的
            current_day_overtime = overtime_hours

            for i, row in enumerate(data_rows):
                try:
                    # 跳过我们正在处理的当前行，因为它的加班数据还没写入
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
            
            # 加上今天刚计算出的加班时间
            monthly_total_overtime += current_day_overtime

        # --- 4. 准备批量更新 ---
        update_requests = [
            {
                'range': f'E{row_index}', # 下班时间
                'values': [[off_work_time_str]],
            },
            {
                'range': f'F{row_index}', # 当日加班
                'values': [[f"{overtime_hours:.2f}"]],
            },
            {
                'range': f'G{row_index}', # 本月累计
                'values': [[f"{monthly_total_overtime:.2f}"]],
            }
        ]
        worksheet.batch_update(update_requests, value_input_option='USER_ENTERED')

        # --- 5. 生成建议 (这部分逻辑可以复用 get_daily_suggestion_tool 或简化) ---
        # 为了保持函数独立性，我们在这里重新计算一次未来工作日
        future_workdays = 0
        today_sheet_index = row_index - 2 # 转换为0-based index
        
        if today_sheet_index != -1:
            for i in range(today_sheet_index + 1, len(all_values) -1):
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

        return (f"成功记录下班时间：{off_work_time_str}.\n"
                f"当日加班：{overtime_hours:.2f} 小时.\n"
                f"本月累计加班：{monthly_total_overtime:.2f} 小时.\n\n"
                f"【智能建议】\n{suggestion}")

    except Exception as e:
        import traceback
        return f"记录下班时发生错误: {type(e).__name__} - {e}\n{traceback.format_exc()}"

import calendar

@tool("Populate Month Schedule Tool")
def populate_month_schedule_tool(year: int, month: int) -> str:
    """
    为一个指定的月份填充默认的周一至周五为工作日、周末为休息日的排班表。
    如果某天已经存在于表格中，则会跳过，不会覆盖。
    Args:
        year (int): 目标年份，例如 2024。
        month (int): 目标月份，例如 5。
    """
    try:
        worksheet = _get_worksheet()
        
        # 优化：一次性获取所有已存在的日期，避免重复查询
        existing_dates = set(worksheet.col_values(1))
        
        num_days = calendar.monthrange(year, month)[1]
        new_rows = []
        
        for day in range(1, num_days + 1):
            target_date = date(year, month, day)
            target_date_str = target_date.strftime("%Y-%m-%d")

            # 如果日期已存在，则跳过
            if target_date_str in existing_dates:
                continue

            weekday_str = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][target_date.weekday()]
            # 周一到周五 (weekday 0-4) 是工作日
            is_workday = target_date.weekday() < 5 
            workday_status = "是" if is_workday else "否"
            
            new_row_data = [target_date_str, weekday_str, workday_status, "18:00:00"]
            new_rows.append(new_row_data)

        if not new_rows:
            return f"{year}年{month}月的排班已存在，无需填充。"
            
        # 一次性批量添加所有新行，效率更高
        worksheet.append_rows(new_rows, value_input_option='USER_ENTERED')
        
        return f"成功为 {year}年{month}月 填充了 {len(new_rows)} 天的默认排班！您现在可以进行个性化调整。"
        
    except Exception as e:
        import traceback
        return f"填充月份排班时发生错误: {type(e).__name__} - {e}\n{traceback.format_exc()}"        
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

        cell = None
        try:
            print(f"[LOG] Searching for cell with value '{today_str}' in column 1...")
            cell = worksheet.find(today_str, in_column=1)
            if not cell:
                suggestion = "今天的计划尚未设定，请先规划日程。"
                print(f"[LOG] Cell not found for today. Returning: {suggestion}")
                return suggestion
            print(f"[LOG] Found cell at row: {cell.row}, col: {cell.col}")

            is_workday_value = worksheet.cell(cell.row, 3).value
            print(f"[LOG] Value of 'is_workday' cell (Row {cell.row}, Col 3): '{is_workday_value}' (Type: {type(is_workday_value)})")
            
            if not is_workday_value or str(is_workday_value).strip().lower() != '是':
                suggestion = "根据计划，今天不是工作日，好好休息！"
                print(f"[LOG] Today is not a workday according to the sheet. Returning: {suggestion}")
                return suggestion

        except gspread.CellNotFound:
            suggestion = "今天的计划尚未设定，请先规划日程。 (CellNotFound)"
            print(f"[LOG] gspread.CellNotFound exception. Returning: {suggestion}")
            return suggestion
        except Exception as e:
            suggestion = f"在检查工作日时发生未知错误: {e}"
            print(f"[LOG] An unexpected error occurred while checking for workday: {e}")
            return suggestion

        print("[LOG] Proceeding to overtime calculation for the current month...")
        
        # 获取所有相关列的值，以减少API调用次数
        all_values = worksheet.get_all_values()
        if not all_values or len(all_values) < 2:
            # 如果表格为空或只有表头，则无需计算
            monthly_total_overtime = 0
            print("[LOG] Worksheet is empty or contains only a header. Total overtime is 0.")
        else:
            data_rows = all_values[1:] # 跳过表头

            current_month = today.month
            current_year = today.year
            monthly_total_overtime = 0

            # 列索引（0-based）: A=0, F=5
            date_col_index = 0
            overtime_col_index = 5

            for row in data_rows:
                try:
                    # 确保行中有足够的数据来访问日期和加班列
                    if len(row) > max(date_col_index, overtime_col_index):
                        date_str = row[date_col_index]
                        overtime_str = row[overtime_col_index]
                        
                        # 跳过没有日期或加班数据的行
                        if not date_str or not overtime_str:
                            continue

                        row_date = datetime.strptime(date_str, "%Y-%m-%d").date()

                        # 仅对当前月份的行进行累加
                        if row_date.year == current_year and row_date.month == current_month:
                            monthly_total_overtime += float(overtime_str)
                except (ValueError, TypeError):
                    # 忽略无法解析日期或加班时间的行
                    print(f"[LOG] Skipping row due to parsing error: {row}")
                    continue
        
        print(f"[LOG] Calculated monthly_total_overtime for {current_year}-{current_month}: {monthly_total_overtime}")

        all_dates = worksheet.col_values(1)
        all_workday_flags = worksheet.col_values(3)
        print("[LOG] Fetched all dates (Col 1) and workday flags (Col 3).")

        future_workdays = 0
        today_sheet_index = -1
        if today_str in all_dates:
            today_sheet_index = all_dates.index(today_str)
            print(f"[LOG] Found today's date at index: {today_sheet_index}")
        else:
            print("[LOG] Today's date was not found in the date column. This should not happen if cell was found earlier.")

        if today_sheet_index != -1:
            print("[LOG] Calculating future workdays...")
            current_month = date.today().month
            for i in range(today_sheet_index + 1, len(all_dates)):
                try:
                    future_date = datetime.strptime(all_dates[i], "%Y-%m-%d").date()
                    if future_date.month != current_month:
                        continue # Skip dates not in the current month
                except (ValueError, IndexError):
                    continue # Skip rows with invalid date formats

                if i < len(all_workday_flags) and str(all_workday_flags[i]).strip().lower() == '是':
                    future_workdays += 1
            print(f"[LOG] Calculated future_workdays (within current month): {future_workdays}")
        
        remaining_overtime_budget = 29 - monthly_total_overtime
        print(f"[LOG] Remaining overtime budget: {remaining_overtime_budget}")
        
        total_remaining_workdays = future_workdays + 1
        print(f"[LOG] Total remaining workdays (including today): {total_remaining_workdays}")
        
        suggestion = ""
        # 1. 如果加班预算已经用完或超额
        if remaining_overtime_budget <= 0:
            suggestion = f"加班额度已用完 (剩余 {remaining_overtime_budget:.2f} 小时)，请18:00准时下班！"
        # 2. 如果今天之后还有工作日
        elif future_workdays > 0:
            avg_overtime_per_day = remaining_overtime_budget / total_remaining_workdays
            suggested_off_time_seconds = 18 * 3600 + avg_overtime_per_day * 3600
            h = int(suggested_off_time_seconds // 3600)
            m = int((suggested_off_time_seconds % 3600) // 60)
            suggestion = f"若要均分剩余加班，今天建议在 {h:02d}:{m:02d} 下班。"
        # 3. 如果只剩今天这最后一个工作日
        elif total_remaining_workdays == 1:
            suggested_off_time_seconds = 18 * 3600 + remaining_overtime_budget * 3600
            h = int(suggested_off_time_seconds // 3600)
            m = int((suggested_off_time_seconds % 3600) // 60)
            suggestion = f"今天是本月最后工作日，为用完额度，建议在 {h:02d}:{m:02d} 下班。"
        # 4. 其他意外情况
        else:
            suggestion = "请18:00准时下班，享受生活！"
        
        print(f"[LOG] Final suggestion: {suggestion}")
        return suggestion

    except Exception as e:
        import traceback
        error_message = f"获取建议时发生严重错误: {type(e).__name__} - {e}\n{traceback.format_exc()}"
        print(f"[LOG] --- FATAL ERROR --- \n{error_message}")
        return error_message        