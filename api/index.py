# api/index.py
from flask import Flask, request, jsonify
from crewai import Agent, Task, Crew, Process
from langchain_openai import ChatOpenAI # 或其他LLM
from dotenv import load_dotenv
import os
import sys
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

# --- 新的 API 端点: /api/update-schedule ---
@app.route('/api/update-schedule', methods=['POST'])
def update_schedule():
    data = request.get_json()
    plan_text = data.get('plan')
    if not plan_text:
        return jsonify({"status": "error", "message": "没有提供计划文本"}), 400

    llm = get_llm()

    # Agent 1: 规划师，负责理解自然语言
    planner_agent = Agent(
        role="日程规划解析员",
        goal=f"解析用户的自然语言输入，并将其转换为对 'Update Schedule Tool' 的精确调用。今天是 {date.today().strftime('%Y-%m-%d')}。",
        backstory="你是一个高效的助理，擅长从非结构化的文本中提取关键信息，比如日期和意图（是上班还是休息）。你会处理'明天'、'后天'、'下周三'等相对日期。",
        llm=llm,
        tools=[update_schedule_tool,populate_month_schedule_tool],
        verbose=True
    )

    # Task 1: 解析并更新
    update_task = Task(
    description=f"""
    解析以下用户的日程计划: '{plan_text}'
    你的任务是根据用户的意图，选择一个工具来执行：
    
    1.  如果用户的意图是【修改某一个或几个特定日期】（例如"明天休息"、"下周三上班"），你应该使用 'Update Schedule Tool'。你需要从中解析出 `target_date` 和 `is_workday` 参数。
    
    2.  如果用户的意图是【为一个完整的月份生成默认排班】（例如"填充六月日历"、"生成2024年7月的排班"），你应该使用 'Populate Month Schedule Tool'。你需要从中解析出 `year` 和 `month` 参数。
    
    3.  执行你选择的工具，并返回其最终结果。
    """,
    expected_output="一个确认操作成功的字符串，说明执行了哪个操作以及结果。",
    agent=planner_agent
)
    # 创建并启动规划Crew
    schedule_crew = Crew(
        agents=[planner_agent],
        tasks=[update_task],
        process=Process.sequential
    )
    result = schedule_crew.kickoff()
    return jsonify({"status": "success", "message": result.raw if hasattr(result, 'raw') else result})

# --- 旧的 API 端点: /api/clock-out ---
@app.route('/api/clock-out', methods=['POST'])
def clock_out():
    llm = get_llm()

    # Agent 2: 操作员，负责记录
    operator_agent = Agent(
        role="考勤记录员",
        goal="使用 'Clock Out and Calculate Tool' 来记录今天的下班时间并进行计算。",
        backstory="你是一个严谨的记录员，你的唯一任务就是在被调用时，执行下班打卡工具。",
        llm=llm,
        tools=[clock_out_tool],
        verbose=True
    )

    # Task 2: 记录和计算
    clock_out_task = Task(
        description="用户点击了'我下班啦'按钮。立即使用 'Clock Out and Calculate Tool' 工具。不需要任何参数。",
        expected_output="一个包含下班时间、加班时长和未来建议的完整报告。",
        agent=operator_agent
    )
    
    # 创建并启动记录Crew
    clock_out_crew = Crew(
        agents=[operator_agent],
        tasks=[clock_out_task],
        process=Process.sequential
    )
    result = clock_out_crew.kickoff()
    return jsonify({"status": "success", "message": result.raw if hasattr(result, 'raw') else result})
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