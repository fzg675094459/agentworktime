# api/index.py
from flask import Flask, request, jsonify
from crewai import Agent, Task, Crew, Process
from langchain_openai import ChatOpenAI # 或其他LLM
from dotenv import load_dotenv
import os
import sys
import traceback
import re # 导入正则表达式模块
from datetime import date, timedelta
from tools.google_sheets_tool import update_schedule_tool, clock_out_tool, populate_month_schedule_tool
# 添加项目根目录到sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
# 导入我们新的、分离的工具
from tools.google_sheets_tool import (
    update_schedule_tool, 
    clock_out_tool, 
    populate_month_schedule_tool,
    get_daily_suggestion_tool # <-- 导入新工具
)

load_dotenv()

app = Flask(__name__)

def get_llm():
    return ChatOpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-4o"
    )

# --- 优化后的 API 端点: /api/update-schedule ---
@app.route('/api/update-schedule', methods=['POST'])
def update_schedule():
    data = request.get_json()
    plan_text = data.get('plan')
    if not plan_text:
        return jsonify({"status": "error", "message": "没有提供计划文本"}), 400

    # --- 优化：优先使用规则解析简单命令 ---
    # 匹配 "填充X月日历" 或 "生成YYYY年M月排班"
    populate_match = re.search(r"(?:填充|生成)(?:(\d{4})年)?(\d{1,2})月(?:日历|的排班|排班)?", plan_text)
    if populate_match:
        try:
            year_str, month_str = populate_match.groups()
            year = int(year_str) if year_str else date.today().year
            month = int(month_str)
            
            if not (1 <= month <= 12):
                 return jsonify({"status": "error", "message": "月份必须在1到12之间。"}), 400

            # 直接调用工具，绕过LLM
            result_message = populate_month_schedule_tool.run(year=year, month=month)
            return jsonify({"status": "success", "message": result_message})
        except Exception as e:
            return jsonify({"status": "error", "message": f"处理填充日历命令时出错: {e}"}), 500

    # --- 如果规则不匹配，回退到使用LLM Agent ---
    llm = get_llm()
    planner_agent = Agent(
        role="日程规划解析员",
        goal=f"解析用户的自然语言输入，并将其转换为对 'Update Schedule Tool' 的精确调用。今天是 {date.today().strftime('%Y-%m-%d')}。",
        backstory="你是一个高效的助理，擅长从非结构化的文本中提取关键信息，比如日期和意图（是上班还是休息）。你会处理'明天'、'后天'、'下周三'等相对日期。",
        llm=llm,
        tools=[update_schedule_tool], # 注意：这里只给它Update工具，因为Populate已经由规则处理了
        verbose=True
    )
    update_task = Task(
        description=f"解析以下用户的日程计划: '{plan_text}'。你的任务是使用 'Update Schedule Tool' 来更新单个日期的状态。你需要从中解析出 `target_date` 和 `is_workday` 参数并执行工具。",
        expected_output="一个确认操作成功的字符串，说明执行了哪个操作以及结果。",
        agent=planner_agent
    )
    schedule_crew = Crew(
        agents=[planner_agent],
        tasks=[update_task],
        process=Process.sequential
    )
    result = schedule_crew.kickoff()
    return jsonify({"status": "success", "message": result.raw if hasattr(result, 'raw') else result})

# --- 优化后的 API 端点: /api/clock-out ---
@app.route('/api/clock-out', methods=['POST'])
def clock_out():
    try:
        # 直接调用工具，移除不必要的Agent和Crew开销
        result_message = clock_out_tool.run()
        return jsonify({"status": "success", "message": result_message})
    except Exception as e:
        # 记录详细错误，以便调试
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"执行打卡操作时出错: {e}"}), 500

@app.route('/api/get-suggestion', methods=['GET'])
def get_suggestion():
    # 这个任务很简单，我们可以直接调用工具，甚至不需要Agent
    try:
        suggestion_message = get_daily_suggestion_tool.run()
        return jsonify({"status": "success", "message": suggestion_message})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 主页路由 ---
@app.route('/')
def index():
    with open(os.path.join(os.path.dirname(__file__), '..', 'index.html')) as f:
        return f.read()

# 本地测试启动
if __name__ == '__main__':
    app.run(debug=True, port=3000)