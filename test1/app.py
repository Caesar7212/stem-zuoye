import json
import os
import time
import hmac
import hashlib
import base64
import urllib.parse
from flask import Flask, request, render_template, make_response, jsonify
import datetime
import logging
from dotenv import load_dotenv
import traceback
import re
import threading
import websocket
import ssl
from time import mktime
from datetime import datetime
from urllib.parse import urlencode
from wsgiref.handlers import format_date_time

# 加载环境变量
load_dotenv()

# 配置日志
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("app_debug.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 星火AI配置
APP_ID = os.getenv("XF_APP_ID", "4cabacd2")
API_SECRET = os.getenv("XF_API_SECRET", "OTZmMjQ0NTNhMzEyMGU3ZmQ3NjE5YmRh")
API_KEY = os.getenv("XF_API_KEY", "7d2cd40dd21e530da80e9069c6e155b8")
SPARK_URL = "wss://spark-api.xf-yun.com/v1/x1"  # X1模型WebSocket地址

# 打印环境变量以验证
logger.info(f"APP_ID: {APP_ID}")
logger.info(f"API_KEY: {API_KEY}")
logger.info(f"API_SECRET: {API_SECRET[:4]}...")  # 只显示前4位避免泄露


class SparkWebSocket:
    def __init__(self, app_id, api_key, api_secret, spark_url):
        self.app_id = app_id
        self.api_key = api_key
        self.api_secret = api_secret
        self.spark_url = spark_url
        self.host = urllib.parse.urlparse(spark_url).netloc
        self.path = urllib.parse.urlparse(spark_url).path
        self.ws = None
        self.answer = ""
        self.completed = False
        self.lock = threading.Lock()
        self.event = threading.Event()

    def create_url(self):
        """生成星火API所需的WebSocket连接URL"""
        # 生成RFC1123格式的时间戳
        now = datetime.now()
        date = format_date_time(mktime(now.timetuple()))

        # 拼接字符串
        signature_origin = "host: " + self.host + "\n"
        signature_origin += "date: " + date + "\n"
        signature_origin += "GET " + self.path + " HTTP/1.1"

        # 进行hmac-sha256加密
        signature_sha = hmac.new(
            self.api_secret.encode('utf-8'),
            signature_origin.encode('utf-8'),
            digestmod=hashlib.sha256
        ).digest()

        signature_sha_base64 = base64.b64encode(signature_sha).decode(encoding='utf-8')

        authorization_origin = f'api_key="{self.api_key}", algorithm="hmac-sha256", headers="host date request-line", signature="{signature_sha_base64}"'

        authorization = base64.b64encode(authorization_origin.encode('utf-8')).decode(encoding='utf-8')

        # 将请求的鉴权参数组合为字典
        v = {
            "authorization": authorization,
            "date": date,
            "host": self.host
        }
        # 拼接鉴权参数，生成url
        url = self.spark_url + '?' + urlencode(v)
        logger.debug(f"Generated WebSocket URL: {url}")
        return url

    def gen_params(self, messages):
        """生成请求参数"""
        data = {
            "header": {
                "app_id": self.app_id,
                "uid": "1234",
            },
            "parameter": {
                "chat": {
                    "domain": "x1",  # 修改为x1
                    "temperature": 0.5,
                    "max_tokens": 4096,
                    "stream": True
                }
            },
            "payload": {
                "message": {
                    "text": messages
                }
            }
        }
        return data

    def on_message(self, ws, message):
        """WebSocket消息接收处理"""
        try:
            data = json.loads(message)
            code = data['header']['code']
            if code != 0:
                logger.error(f"API request error: {code}, {data}")
                self.completed = True
                self.event.set()
                return

            choices = data["payload"]["choices"]
            status = choices["status"]
            # 检查 text 列表是否存在且至少有一个元素，以及元素中是否有 content 键
            if "text" in choices and len(choices["text"]) > 0 and "content" in choices["text"][0]:
                content = choices["text"][0]["content"]
                with self.lock:
                    self.answer += content
            else:
                logger.warning("Message does not contain 'content' key.")

            # 状态为2表示结束
            if status == 2:
                logger.debug("API response completed")
                self.completed = True
                self.event.set()
        except Exception as e:
            logger.error(f"Error processing WebSocket message: {str(e)}")
            self.completed = True
            self.event.set()

    def on_error(self, ws, error):
        """WebSocket错误处理"""
        logger.error(f"WebSocket error: {error}")
        self.completed = True
        self.event.set()

    def on_close(self, ws, close_status_code, close_msg):
        """WebSocket连接关闭处理"""
        logger.debug("WebSocket connection closed")
        self.completed = True
        self.event.set()

    def on_open(self, ws):
        """WebSocket连接打开处理"""
        logger.debug("WebSocket connection opened")
        data = json.dumps(self.gen_params(self.messages))
        ws.send(data)
        logger.debug(f"Sent request: {data}")

    def call_spark_api(self, messages):
        """调用星火API（流式版本）"""
        self.messages = messages
        self.answer = ""
        self.completed = False
        self.event.clear()

        ws_url = self.create_url()

        # 创建WebSocket连接 - 使用正确的回调绑定方式
        self.ws = websocket.WebSocketApp(
            ws_url,
            on_message=self.on_message,  # 直接引用类方法
            on_error=self.on_error,  # 直接引用类方法
            on_close=self.on_close,  # 直接引用类方法
            on_open=self.on_open  # 直接引用类方法
        )

        # 在新线程中运行WebSocket
        wst = threading.Thread(target=self.ws.run_forever, kwargs={
            "sslopt": {"cert_reqs": ssl.CERT_NONE}
        })
        wst.daemon = True
        wst.start()

        # 等待响应完成或超时
        self.event.wait(timeout=60)  # 60秒超时

        if not self.completed:
            logger.warning("API call timed out")
            self.ws.close()

        # 清理HTML内容
        cleaned_content = re.sub(r'^[^{<]*', '', self.answer, flags=re.DOTALL)
        cleaned_content = re.sub(r'[^>]*$', '', cleaned_content, flags=re.DOTALL)

        return cleaned_content

# 创建SparkWebSocket实例
spark_ws = SparkWebSocket(APP_ID, API_KEY, API_SECRET, SPARK_URL)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/generate_mindmap', methods=['POST'])
def generate_mindmap():
    try:
        logger.info("收到思维导图生成请求")

        # 获取并验证JSON数据
        data = request.get_json()
        if not data:
            logger.error("请求中缺少JSON数据")
            return jsonify({"error": "请求中缺少JSON数据"}), 400

        # 提取参数
        course_title = data.get('course_title', '').strip()
        course_description = data.get('course_description', '').strip()
        teaching_objectives = data.get('teaching_objectives', '').strip()

        logger.info(f"课程标题: '{course_title}'")
        logger.info(f"课程描述: '{course_description[:50]}...'" if course_description else "无课程描述")
        logger.info(f"教学目标: '{teaching_objectives[:50]}...'" if teaching_objectives else "无教学目标")

        # 验证必要参数
        if not course_title:
            logger.error("课程标题不能为空")
            return jsonify({"error": "课程标题不能为空"}), 400

        # 构建思维导图生成的prompt
        mindmap_prompt = [
            {
                "role": "system",
                "content": (
                    "你是一个专业的教育辅助AI，擅长生成课程思维导图。"
                    "请根据用户提供的信息生成课程思维导图的HTML代码。"
                    "输出格式要求："
                    "1. 只输出HTML代码，不要包含任何解释"
                    "2. 使用<div class='mindmap'>作为容器"
                    "3. 使用<h3>表示主要主题"
                    "4. 使用<ul>和<li>表示层级结构"
                    "5. 不要包含任何CSS或JavaScript"
                    "6. 思维导图应该包含以下部分: "
                    "   - 课程介绍"
                    "   - 教学目标"
                    "   - 核心概念"
                    "   - 关键知识点"
                    "   - 教学方法"
                    "   - 评估方式"
                )
            },
            {"role": "user", "content": f"课程标题: {course_title}"}
        ]

        if course_description:
            mindmap_prompt.append({"role": "user", "content": f"课程描述: {course_description}"})
        if teaching_objectives:
            mindmap_prompt.append({"role": "user", "content": f"教学目标: {teaching_objectives}"})

        # 添加格式示例
        mindmap_prompt.append({
            "role": "user",
            "content": "示例格式：<div class='mindmap'><h3>主题1</h3><ul><li>子主题1.1</li></ul></div>"
        })

        logger.info("调用星火API生成思维导图...")
        mindmap = spark_ws.call_spark_api(mindmap_prompt)

        if mindmap:
            logger.info(f"成功生成思维导图，长度: {len(mindmap)}")

            # 验证HTML格式
            if mindmap.startswith('<div') and mindmap.endswith('</div>'):
                logger.info("思维导图格式验证通过")
                return jsonify({
                    "mindmap": mindmap,
                    "course_title": course_title,
                    "course_description": course_description,
                    "teaching_objectives": teaching_objectives
                })
            else:
                logger.warning(f"无效的思维导图格式: {mindmap[:100]}...")
                return jsonify({
                    "error": "AI返回了无效的思维导图格式",
                    "content": mindmap[:500] + "..." if len(mindmap) > 500 else mindmap
                }), 500
        else:
            logger.error("生成思维导图失败")
            return jsonify({"error": "生成思维导图失败，请重试"}), 500

    except Exception as e:
        logger.exception(f"生成思维导图时发生异常: {str(e)}\n{traceback.format_exc()}")
        return jsonify({
            "error": "服务器内部错误",
            "details": str(e),
            "traceback": traceback.format_exc()
        }), 500


@app.route('/generate_teaching_flow', methods=['POST'])
def generate_teaching_flow():
    try:
        logger.info("收到教学流程生成请求")

        # 获取并验证JSON数据
        data = request.get_json()
        if not data:
            logger.error("请求中缺少JSON数据")
            return jsonify({"error": "请求中缺少JSON数据"}), 400

        # 提取参数
        mindmap = data.get('mindmap', '').strip()
        course_title = data.get('course_title', '课程').strip()

        logger.info(f"课程标题: '{course_title}'")
        logger.info(f"思维导图长度: {len(mindmap)}")

        # 验证必要参数
        if not mindmap:
            logger.error("思维导图内容不能为空")
            return jsonify({"error": "请先生成思维导图"}), 400

        # 构建教学流程生成的prompt
        flow_prompt = [
            {
                "role": "system",
                "content": (
                    "你是一个专业的教育设计师，擅长根据思维导图生成详细的教学流程。"
                    "请根据提供的思维导图生成教学流程的HTML代码。"
                    "输出格式要求："
                    "1. 只输出HTML代码，不要包含任何解释"
                    "2. 使用<div class='teaching-flow'>作为容器"
                    "3. 使用<h3>表示教学环节"
                    "4. 使用<p class='time'>表示时间分配"
                    "5. 使用<ul>表示教学活动"
                    "6. 使用<ol>表示教学资源需求"
                    "7. 不要包含任何CSS或JavaScript"
                    "8. 教学流程应该包含以下环节: "
                    "   - 导入环节"
                    "   - 知识讲解"
                    "   - 互动活动"
                    "   - 练习与实践"
                    "   - 总结与评价"
                )
            },
            {"role": "user", "content": f"课程标题: {course_title}"},
            {"role": "user", "content": f"思维导图内容：\n{mindmap}"},
            {
                "role": "user",
                "content": "示例格式：<div class='teaching-flow'><h3>一、导入环节</h3><p class='time'>时间：5分钟</p><ul><li>活动1</li></ul><p>所需资源：</p><ol><li>资源1</li></ol></div>"
            }
        ]

        logger.info("调用星火API生成教学流程...")
        teaching_flow = spark_ws.call_spark_api(flow_prompt)

        if teaching_flow:
            logger.info(f"成功生成教学流程，长度: {len(teaching_flow)}")
            return jsonify({"teaching_flow": teaching_flow})
        else:
            logger.error("生成教学流程失败")
            return jsonify({"error": "生成教学流程失败，请重试"}), 500

    except Exception as e:
        logger.exception(f"生成教学流程时发生异常: {str(e)}\n{traceback.format_exc()}")
        return jsonify({
            "error": "服务器内部错误",
            "details": str(e),
            "traceback": traceback.format_exc()
        }), 500


@app.route('/generate_problem_simulation', methods=['POST'])
def generate_problem_simulation():
    try:
        logger.info("收到问题模拟生成请求")

        # 获取并验证JSON数据
        data = request.get_json()
        if not data:
            logger.error("请求中缺少JSON数据")
            return jsonify({"error": "请求中缺少JSON数据"}), 400

        # 提取参数
        teaching_flow = data.get('teaching_flow', '').strip()
        course_title = data.get('course_title', '课程').strip()

        logger.info(f"课程标题: '{course_title}'")
        logger.info(f"教学流程长度: {len(teaching_flow)}")

        # 验证必要参数
        if not teaching_flow:
            logger.error("教学流程内容不能为空")
            return jsonify({"error": "请先生成教学流程"}), 400

        # 构建问题模拟生成的prompt
        simulation_prompt = [
            {
                "role": "system",
                "content": (
                    "你是一个经验丰富的教育分析师，擅长根据教学流程进行问题模拟与预期效果分析。"
                    "请根据提供的教学流程生成问题模拟的HTML代码。"
                    "输出格式要求："
                    "1. 只输出HTML代码，不要包含任何解释"
                    "2. 使用<div class='problem-simulation'>作为容器"
                    "3. 使用<h4>表示各部分标题"
                    "4. 使用<ul>表示列表内容"
                    "5. 使用<script id='chart-data'>包含雷达图数据"
                    "6. 不要包含任何CSS或JavaScript（除了雷达图数据）"
                    "7. 内容应包含以下部分: "
                    "   - 可能出现的问题及解决方案"
                    "   - 学生可能提出的疑问"
                    "   - 预期学习效果"
                    "   - 教学难点分析"
                )
            },
            {"role": "user", "content": f"课程标题: {course_title}"},
            {"role": "user", "content": f"教学流程内容：\n{teaching_flow}"},
            {
                "role": "user",
                "content": "示例格式：<div class='problem-simulation'><h4>可能出现的问题</h4><ul><li>问题1</li></ul><h4>预期学习效果</h4><ul><li>效果1</li></ul><script id='chart-data'>{'labels':[...], 'datasets':[...]}</script></div>"
            }
        ]

        logger.info("调用星火API生成问题模拟...")
        simulation = spark_ws.call_spark_api(simulation_prompt)

        if simulation:
            logger.info(f"成功生成问题模拟，长度: {len(simulation)}")
            return jsonify({"simulation": simulation})
        else:
            logger.error("生成问题模拟失败")
            return jsonify({"error": "生成问题模拟失败，请重试"}), 500

    except Exception as e:
        logger.exception(f"生成问题模拟时发生异常: {str(e)}\n{traceback.format_exc()}")
        return jsonify({
            "error": "服务器内部错误",
            "details": str(e),
            "traceback": traceback.format_exc()
        }), 500


@app.route('/generate_evaluation', methods=['POST'])
def generate_evaluation():
    try:
        logger.info("收到教学评价生成请求")

        # 获取并验证JSON数据
        data = request.get_json()
        if not data:
            logger.error("请求中缺少JSON数据")
            return jsonify({"error": "请求中缺少JSON数据"}), 400

        # 提取参数
        simulation = data.get('simulation', '').strip()
        course_title = data.get('course_title', '课程').strip()

        logger.info(f"课程标题: '{course_title}'")
        logger.info(f"问题模拟长度: {len(simulation)}")

        # 验证必要参数
        if not simulation:
            logger.error("问题模拟内容不能为空")
            return jsonify({"error": "请先生成问题模拟"}), 400

        # 构建评价生成的prompt
        evaluation_prompt = [
            {
                "role": "system",
                "content": (
                    "你是一个资深教育评估专家，擅长对教学设计进行全面评价并提供优化建议。"
                    "请根据提供的问题模拟生成评价的HTML代码。"
                    "输出格式要求："
                    "1. 只输出HTML代码，不要包含任何解释"
                    "2. 使用<div class='evaluation'>作为容器"
                    "3. 使用<h4>表示各部分标题"
                    "4. 使用<ul>表示列表内容"
                    "5. 使用<div id='rating-data'>包含评分数据"
                    "6. 不要包含任何CSS或JavaScript"
                    "7. 内容应包含以下部分: "
                    "   - 教学设计优点"
                    "   - 教学设计改进点"
                    "   - 整体评分（1-10分）"
                    "   - 具体优化建议"
                )
            },
            {"role": "user", "content": f"课程标题: {course_title}"},
            {"role": "user", "content": f"问题模拟内容：\n{simulation}"},
            {
                "role": "user",
                "content": "示例格式：<div class='evaluation'><h4>教学设计优点</h4><ul><li>优点1</li></ul><h4>教学设计改进点</h4><ul><li>改进点1</li></ul><div id='rating-data'>{'score': 4, 'description': '评价描述'}</div></div>"
            }
        ]

        logger.info("调用星火API生成教学评价...")
        evaluation = spark_ws.call_spark_api(evaluation_prompt)

        if evaluation:
            logger.info(f"成功生成教学评价，长度: {len(evaluation)}")
            return jsonify({"evaluation": evaluation})
        else:
            logger.error("生成教学评价失败")
            return jsonify({"error": "生成教学评价失败，请重试"}), 500

    except Exception as e:
        logger.exception(f"生成教学评价时发生异常: {str(e)}\n{traceback.format_exc()}")
        return jsonify({
            "error": "服务器内部错误",
            "details": str(e),
            "traceback": traceback.format_exc()
        }), 500


@app.route('/export_mindmap', methods=['POST'])
def export_mindmap():
    try:
        logger.info("收到思维导图导出请求")

        # 获取表单数据
        mindmap = request.form.get('mindmap', '').strip()
        course_title = request.form.get('course_title', '未命名课程').strip()

        logger.info(f"课程标题: '{course_title}'")
        logger.info(f"思维导图长度: {len(mindmap)}")

        if not mindmap:
            logger.error("没有可导出的思维导图内容")
            return jsonify({"error": "没有可导出的思维导图"}), 400

        # 创建完整的HTML文档
        html_content = f"""
        <!DOCTYPE html>
        <html lang="zh-CN">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>{course_title} - 思维导图</title>
            <style>
                body {{ 
                    font-family: 'Microsoft YaHei', sans-serif; 
                    padding: 20px; 
                    max-width: 800px; 
                    margin: 0 auto; 
                    line-height: 1.6;
                    color: #333;
                }}
                h1 {{ 
                    color: #165DFF; 
                    text-align: center;
                    margin-bottom: 20px;
                    padding-bottom: 10px;
                    border-bottom: 2px solid #dbeafe;
                }}
                .mindmap-container {{ 
                    background-color: #f8f9fa; 
                    padding: 20px; 
                    border-radius: 8px; 
                    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                }}
                h3 {{ 
                    color: #1d4ed8; 
                    margin-top: 20px; 
                    padding-bottom: 5px; 
                    border-bottom: 1px solid #dbeafe;
                }}
                ul {{ 
                    list-style-type: none; 
                    padding-left: 20px; 
                }}
                li {{ 
                    margin: 8px 0; 
                    position: relative; 
                    padding-left: 15px; 
                }}
                li:before {{ 
                    content: "•"; 
                    position: absolute; 
                    left: 0; 
                    color: #3b82f6; 
                    font-weight: bold;
                }}
                ul ul {{ 
                    border-left: 1px dashed #93c5fd; 
                    margin-left: 10px; 
                }}
                .footer {{ 
                    text-align: center; 
                    margin-top: 30px; 
                    color: #6b7280; 
                    font-size: 0.9em;
                }}
            </style>
        </head>
        <body>
            <h1>{course_title}</h1>
            <div class="mindmap-container">
                {mindmap}
            </div>
            <div class="footer">
                生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}
            </div>
        </body>
        </html>
        """

        # 创建响应
        response = make_response(html_content)
        # 创建安全文件名
        safe_filename = f"{course_title.replace(' ', '_').replace('/', '_')}_思维导图.html"
        response.headers['Content-Disposition'] = f'attachment; filename="{safe_filename}"'
        response.headers['Content-Type'] = 'text/html'

        logger.info("成功导出思维导图")
        return response

    except Exception as e:
        logger.exception(f"导出思维导图时发生异常: {str(e)}\n{traceback.format_exc()}")
        return jsonify({
            "error": "导出失败",
            "details": str(e),
            "traceback": traceback.format_exc()
        }), 500


@app.route('/export_full_plan', methods=['POST'])
def export_full_plan():
    try:
        logger.info("收到完整教案导出请求")

        # 获取表单数据
        course_title = request.form.get('course_title', '未命名课程').strip()
        course_description = request.form.get('course_description', '').strip()
        teaching_objectives = request.form.get('teaching_objectives', '').strip()
        mindmap = request.form.get('mindmap', '').strip()
        teaching_flow = request.form.get('teaching_flow', '').strip()
        simulation = request.form.get('simulation', '').strip()
        evaluation = request.form.get('evaluation', '').strip()

        logger.info(f"课程标题: '{course_title}'")
        logger.info(f"思维导图长度: {len(mindmap)}")
        logger.info(f"教学流程长度: {len(teaching_flow)}")
        logger.info(f"问题模拟长度: {len(simulation)}")
        logger.info(f"教学评价长度: {len(evaluation)}")

        # 验证内容完整性
        if not (mindmap and teaching_flow and simulation and evaluation):
            missing = []
            if not mindmap: missing.append("思维导图")
            if not teaching_flow: missing.append("教学流程")
            if not simulation: missing.append("问题模拟")
            if not evaluation: missing.append("教学评价")

            logger.error(f"教案内容不完整，缺少: {', '.join(missing)}")
            return jsonify({"error": f"教案内容不完整，缺少: {', '.join(missing)}"}), 400

        # 创建完整的HTML文档
        html_content = f"""
        <!DOCTYPE html>
        <html lang="zh-CN">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>{course_title} - 完整教案</title>
            <style>
                body {{
                    font-family: 'Microsoft YaHei', sans-serif;
                    padding: 20px;
                    max-width: 1000px;
                    margin: 0 auto;
                    line-height: 1.6;
                    color: #333;
                }}
                h1 {{
                    color: #165DFF;
                    text-align: center;
                    padding-bottom: 10px;
                    border-bottom: 2px solid #dbeafe;
                    margin-bottom: 30px;
                }}
                h2 {{
                    color: #1d4ed8;
                    margin-top: 40px;
                    padding-bottom: 5px;
                    border-bottom: 1px solid #dbeafe;
                }}
                .section {{
                    background-color: #f8f9fa;
                    padding: 25px;
                    border-radius: 10px;
                    margin-bottom: 30px;
                    box-shadow: 0 2px 5px rgba(0,0,0,0.05);
                }}
                .info-box {{
                    background-color: #e7f3ff;
                    border-left: 6px solid #2196F3;
                    padding: 15px;
                    margin-bottom: 30px;
                }}
                h3 {{
                    color: #2563eb;
                    margin-top: 25px;
                }}
                h4 {{
                    color: #3b82f6;
                    margin-top: 20px;
                }}
                ul, ol {{
                    padding-left: 25px;
                }}
                li {{
                    margin-bottom: 10px;
                }}
                .time {{
                    font-weight: bold;
                    color: #ef4444;
                }}
                .chart-placeholder {{
                    background-color: #f1f5f9;
                    padding: 20px;
                    text-align: center;
                    border-radius: 5px;
                    margin: 15px 0;
                    font-style: italic;
                    color: #6b7280;
                }}
                .summary {{
                    background-color: #e7f3ff;
                    padding: 20px;
                    border-radius: 10px;
                    text-align: center;
                    margin-top: 30px;
                }}
                .footer {{
                    text-align: center;
                    margin-top: 40px;
                    padding-top: 20px;
                    border-top: 1px solid #eee;
                    color: #6b7280;
                    font-size: 0.9em;
                }}
            </style>
        </head>
        <body>
            <h1>{course_title} - 完整教案</h1>

            <div class="info-box">
                <h3>课程描述</h3>
                <p>{course_description or '无描述'}</p>

                <h3>教学目标</h3>
                <p>{teaching_objectives or '无目标描述'}</p>
            </div>

            <div class="section">
                <h2>一、思维导图</h2>
                {mindmap}
            </div>

            <div class="section">
                <h2>二、教学流程</h2>
                {teaching_flow}
            </div>

            <div class="section">
                <h2>三、问题模拟与预期效果</h2>
                {simulation.replace('<script id="chart-data">', '<div class="chart-placeholder">图表数据（在网页版中显示）</div><script id="chart-data" disabled>')}
            </div>

            <div class="section">
                <h2>四、评价与优化建议</h2>
                {evaluation}
            </div>

            <div class="summary">
                <h3>教案总结</h3>
                <p>本教案由AI辅助生成，设计完整，具有良好的可操作性和创新性。</p>
                <p>生成时间：{datetime.datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}</p>
            </div>

            <div class="footer">
                {course_title} - 完整教案 &copy; {datetime.datetime.now().year}
            </div>
        </body>
        </html>
        """

        # 创建响应
        response = make_response(html_content)
        # 创建安全文件名
        safe_filename = f"{course_title.replace(' ', '_').replace('/', '_')}_完整教案.html"
        response.headers['Content-Disposition'] = f'attachment; filename="{safe_filename}"'
        response.headers['Content-Type'] = 'text/html'

        logger.info("成功导出完整教案")
        return response

    except Exception as e:
        logger.exception(f"导出完整教案时发生异常: {str(e)}\n{traceback.format_exc()}")
        return jsonify({
            "error": "导出失败",
            "details": str(e),
            "traceback": traceback.format_exc()
        }), 500


@app.route('/health', methods=['GET'])
def health_check():
    """健康检查端点"""
    return jsonify({
        "status": "ok",
        "timestamp": datetime.datetime.now().isoformat(),
        "app": "Teaching Assistant Backend",
        "version": "1.0.0"
    })


if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    debug_mode = os.getenv("DEBUG", "false").lower() == "true"

    # 添加启动消息
    logger.info(f"启动应用，端口: {port}, 调试模式: {debug_mode}")
    logger.info("星火API配置:")
    logger.info(f"  APP_ID: {APP_ID}")
    logger.info(f"  API_KEY: {API_KEY[:6]}...")
    logger.info(f"  API_SECRET: {API_SECRET[:6]}...")

    app.run(host='0.0.0.0', port=port, debug=debug_mode)