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
    """
    try:
        worksheet = _get_worksheet()
        today_str = date.today().strftime("%Y-%m-%d")
        row_index = _find_or_create_row(worksheet, today_str)

        is_workday = worksheet.cell(row_index, 3).value
        if not is_workday or is_workday.lower() != '是':
            return f"根据你的计划，今天 ({today_str}) 不是工作日，无需记录下班。"

        now = datetime.now()
        off_work_time_str = now.strftime("%H:%M:%S")
        worksheet.update_cell(row_index, 5, off_work_time_str)

        standard_off_time_str = worksheet.cell(row_index, 4).value
        standard_off_time = datetime.strptime(standard_off_time_str, "%H:%M:%S").time()
        actual_off_time = now.time()
        overtime_delta = datetime.combine(date.today(), actual_off_time) - datetime.combine(date.today(), standard_off_time)
        overtime_hours = max(0, overtime_delta.total_seconds() / 3600)
        worksheet.update_cell(row_index, 6, f"{overtime_hours:.2f}")

        all_overtime_values = worksheet.col_values(6)[1:]
        monthly_total_overtime = sum(float(v) for v in all_overtime_values if v)
        worksheet.update_cell(row_index, 7, f"{monthly_total_overtime:.2f}")

        all_dates = worksheet.col_values(1)
        all_workday_flags = worksheet.col_values(3)
        future_workdays = 0
        
        today_sheet_index = -1
        if today_str in all_dates:
            today_sheet_index = all_dates.index(today_str)
        
        if today_sheet_index != -1:
            for i in range(today_sheet_index + 1, len(all_dates)):
                if i < len(all_workday_flags) and all_workday_flags[i] and all_workday_flags[i].lower() == '是':
                    future_workdays += 1
        
        remaining_overtime_budget = 29 - monthly_total_overtime
        suggestion = ""

        if monthly_total_overtime >= 29:
            suggestion = f"警告！你本月的加班时长已达 {monthly_total_overtime:.2f} 小时，已超出29小时的额度！接下来请务必准时下班！"
        elif future_workdays > 0 and remaining_overtime_budget > 0:
            avg_overtime_per_day = remaining_overtime_budget / future_workdays
            suggested_off_time_seconds = 18 * 3600 + avg_overtime_per_day * 3600
            h = int(suggested_off_time_seconds // 3600)
            m = int((suggested_off_time_seconds % 3600) // 60)
            suggestion = f"根据计划，你本月还有 {future_workdays} 个工作日。为了不超过29小时总加班，你接下来平均需要加班 {avg_overtime_per_day:.2f} 小时，建议在 {h:02d}:{m:02d} 左右下班。"
        elif future_workdays > 0 and remaining_overtime_budget <= 0:
            suggestion = "太棒了！你的加班额度已经用完或有富余。接下来请准时下班，享受生活！"
        else:
            suggestion = "本月已无剩余工作日，请好好休息！"

        return (f"成功记录下班时间：{off_work_time_str}。\n"
                f"当日加班：{overtime_hours:.2f} 小时。\n"
                f"本月累计加班：{monthly_total_overtime:.2f} 小时。\n\n"
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
    try:
        today = date.today()
        # 首先，直接判断今天是不是周末
        if today.weekday() >= 5: # 5是周六, 6是周日
            return "今天是周末，好好休息吧！"

        worksheet = _get_worksheet()
        today_str = today.strftime("%Y-%m-%d")

        # 检查今天的计划是否已设定，以及是否为工作日
        try:
            cell = worksheet.find(today_str, in_column=1)
            if not cell: # 如果找不到今天的日期行
                return "今天的计划尚未设定，请先规划日程。"
            is_workday_value = worksheet.cell(cell.row, 3).value
            if not is_workday_value or is_workday_value.lower() != '是':
                return "根据计划，今天不是工作日，好好休息！"
        except gspread.CellNotFound:
             # 找不到日期也意味着计划未设定
            return "今天的计划尚未设定，请先规划日程。"
        except AttributeError:
            # 单元格为空值等情况
            return "今天的计划尚未设定或格式不正确，请检查表格。"


        # 计算累计加班和未来工作日
        all_overtime_values = worksheet.col_values(6)[1:]
        monthly_total_overtime = sum(float(v) for v in all_overtime_values if v and v.replace('.', '', 1).isdigit())

        all_dates = worksheet.col_values(1)
        all_workday_flags = worksheet.col_values(3)
        future_workdays = 0
        
        today_sheet_index = -1
        if today_str in all_dates:
            today_sheet_index = all_dates.index(today_str)

        # 注意：这里计算未来工作日时，不包含今天
        if today_sheet_index != -1:
            for i in range(today_sheet_index + 1, len(all_dates)):
                if i < len(all_workday_flags) and all_workday_flags[i] and all_workday_flags[i].lower() == '是':
                    future_workdays += 1
        
        remaining_overtime_budget = 29 - monthly_total_overtime
        
        # 计算今天的建议下班时间
        # 假设今天的加班时间也要均分到剩余天数里 (包括今天)
        total_remaining_workdays = future_workdays + 1 # 未来工作日 + 今天
        
        if monthly_total_overtime >= 29:
            return "加班已满，请18:00准时下班！"

        if total_remaining_workdays > 0 and remaining_overtime_budget > 0:
            avg_overtime_per_day = remaining_overtime_budget / total_remaining_workdays
            suggested_off_time_seconds = 18 * 3600 + avg_overtime_per_day * 3600
            h = int(suggested_off_time_seconds // 3600)
            m = int((suggested_off_time_seconds % 3600) // 60)
            return f"若要均分剩余加班，今天建议在 {h:02d}:{m:02d} 下班。"
        else:
            return "请18:00准时下班，享受生活！"

    except Exception as e:
        import traceback
        return f"获取建议时出错: {e}"        