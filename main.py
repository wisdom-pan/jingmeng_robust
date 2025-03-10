import datetime
import json
import time

import requests
from flask import Flask, render_template, request, session
import os
import uuid
from LRU_cache import LRUCache
import threading
import pickle
import asyncio
import yaml

import os
from langchain.embeddings.openai import OpenAIEmbeddings
from langchain.text_splitter import CharacterTextSplitter
from langchain.chains import RetrievalQA
# from langchain.llms import ChatOpenAI as OpenAI
from langchain.chat_models import ChatOpenAI as OpenAI
from langchain.vectorstores import Chroma
from langchain.document_loaders import PyPDFLoader
from langchain.document_loaders import Docx2txtLoader
from rouge import Rouge

import openai as BaseOpenAI

from langchain.document_loaders.base import BaseLoader
from langchain.docstore.document import Document

from langchain.prompts import PromptTemplate
prompt_template = """使用以下 文本 来回答最后的 问题。
如果你不知道答案，只回答"未找到答案"，不要编造答案。
如果你的答案不是来自 文本 ，只回答"未找到答案"，不要根据你已有的知识回答。
答案应该尽量流畅自然，答案应该尽量完整。
你必须使用中文回答。

文本: {context}

问题: {question}
中文答案:"""
PROMPT = PromptTemplate(
    template=prompt_template, input_variables=["context", "question"]
)

class json_loader(BaseLoader):

    def __init__(self) -> None:
        super().__init__()
    def load(self):
        docs = []
        for i in range(5):
            f = open('json_data/new_json_{}.json'.format(str(i)), 'r',encoding='utf-8')
            content = f.read()
            content = json.loads(content)
            for item in content['custom']['infoList']:
                doc = Document(page_content=item['kinfoName']+'\n\n'+item['kinfoContent'],metadata={'url':"https://www.jingmen.gov.cn/col/col18658/index.html?kinfoGuid="+item['kinfoGuid'],'title':item['kinfoName']})
                docs.append(doc)
        return docs
    
class wx_loader(BaseLoader):

    def __init__(self) -> None:
        super().__init__()
    def load(self):
        docs = []
        for filename in os.listdir("wx_json"):
            # skip questions folder
            if os.path.isdir(f'wx_json/{filename}'):
                continue
            f = open('wx_json/'+filename, 'r',encoding='utf-8')
            content = f.read()
            content = json.loads(content)
            doc=Document(page_content=content['title']+'\n\n'+content['content'],metadata={'url':content['url'],'title':content['title']})
            # 检测 doc 长度
            if len(doc.page_content) > 1000:
                print(f'wx_json/{filename} is too long, {doc}')
            docs.append(doc)
        return docs

class txt_loader(BaseLoader):

    def __init__(self) -> None:
        super().__init__()
    def load(self):
        docs = []
        for i in range(305):
            f = open('json_data/new_json_{}.json'.format(str(i)), 'r')
            content = f.read()
            content = json.loads(content)
            for item in content['custom']['infoList']:
                doc = Document(page_content=item['kinfoName']+'\n\n'+item['kinfoContent'],metadata={'url':"https://www.jingmen.gov.cn/col/col18658/index.html?kinfoGuid="+item['kinfoGuid']})
                docs.append(doc)
        return docs

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)
UPLOAD_FOLDER = './uploads'  #文件存放路径
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.load(f, Loader=yaml.FullLoader)
    if 'HTTPS_PROXY' in config:
        if os.environ.get('HTTPS_PROXY') is None:   # 优先使用环境变量中的代理，若环境变量中没有代理，则使用配置文件中的代理
            os.environ['HTTPS_PROXY'] = config['HTTPS_PROXY']
    PORT = config['PORT']
    CHAT_CONTEXT_NUMBER_MAX = config['CHAT_CONTEXT_NUMBER_MAX']     # 连续对话模式下的上下文最大数量 n，即开启连续对话模式后，将上传本条消息以及之前你和GPT对话的n-1条消息
    USER_SAVE_MAX = config['USER_SAVE_MAX']     # 设置最多存储n个用户，当用户过多时可适当调大

if os.getenv("DEPLOY_ON_RAILWAY") is not None:  # 如果是在Railway上部署，需要删除代理
    os.environ.pop('HTTPS_PROXY', None)

if os.getenv("OPENAI_API_KEY") is not None:  # 如果是在Railway上部署，需要删除代理
    print('true')
    print(os.getenv("OPENAI_API_KEY"))
else:
    print('false')

API_KEY = os.getenv("OPENAI_API_KEY")  # 如果环境变量中设置了OPENAI_API_KEY，则使用环境变量中的OPENAI_API_KEY
API_KEY = API_KEY.split('[SEP]')
PORT = os.getenv("PORT", default=PORT)  # 如果环境变量中设置了PORT，则使用环境变量中的PORT

STREAM_FLAG = False  # 是否开启流式推送
USER_DICT_FILE = "all_user_dict_v2.pkl"  # 用户信息存储文件（包含版本）
lock = threading.Lock()  # 用于线程锁

project_info = "## 智能客服对话机器人    \n" \
               "发送`帮助`可获取帮助  \n"
def get_response_from_ChatGPT_API(message_context, apikey):
    """
    从ChatGPT API获取回复
    :param apikey:
    :param message_context: 上下文
    :return: 回复
    """
    if apikey is None:
        apikey = API_KEY

    header = {"Content-Type": "application/json",
              "Authorization": "Bearer " + apikey}

    data = {
        "model": "gpt-3.5-turbo",
        "messages": message_context
    }
    url = "https://api.openai.com/v1/chat/completions"
    try:
        response = requests.post(url, headers=header, data=json.dumps(data))
        response = response.json()
        # 判断是否含 choices[0].message.content
        if "choices" in response \
                and len(response["choices"]) > 0 \
                and "message" in response["choices"][0] \
                and "content" in response["choices"][0]["message"]:
            data = response["choices"][0]["message"]["content"]
        else:
            data = str(response)

    except Exception as e:
        print(e)
        return str(e)

    return data


def get_message_context(message_history, have_chat_context, chat_with_history):
    """
    获取上下文
    :param message_history:
    :param have_chat_context:
    :param chat_with_history:
    :return:
    """
    message_context = []
    total = 0
    if chat_with_history:
        num = min([len(message_history), CHAT_CONTEXT_NUMBER_MAX, have_chat_context])
        # 获取所有有效聊天记录
        valid_start = 0
        valid_num = 0
        for i in range(len(message_history) - 1, -1, -1):
            message = message_history[i]
            if message['role'] in {'assistant', 'user'}:
                valid_start = i
                valid_num += 1
            if valid_num >= num:
                break

        for i in range(valid_start, len(message_history)):
            message = message_history[i]
            if message['role'] in {'assistant', 'user'}:
                message_context.append(message)
                total += len(message['content'])
    else:
        message_context.append(message_history[-1])
        total += len(message_history[-1]['content'])

    print(f"len(message_context): {len(message_context)} total: {total}",)
    return message_context


def handle_messages_get_response(message, apikey, message_history, have_chat_context, chat_with_history):
    """
    处理用户发送的消息，获取回复
    :param message: 用户发送的消息
    :param apikey:
    :param message_history: 消息历史
    :param have_chat_context: 已发送消息数量上下文(从重置为连续对话开始)
    :param chat_with_history: 是否连续对话
    """
    message_history.append({"role": "user", "content": message})
    message_context = get_message_context(message_history, have_chat_context, chat_with_history)
    response = get_response_from_ChatGPT_API(message_context, apikey)
    message_history.append({"role": "assistant", "content": response})
    # 换行打印messages_history
    # print("message_history:")
    # for i, message in enumerate(message_history):
    #     if message['role'] == 'user':
    #         print(f"\t{i}:\t{message['role']}:\t\t{message['content']}")
    #     else:
    #         print(f"\t{i}:\t{message['role']}:\t{message['content']}")

    return response


def get_response_stream_generate_from_ChatGPT_API(message_context, apikey, message_history):
    """
    从ChatGPT API获取回复
    :param apikey:
    :param message_context: 上下文
    :return: 回复
    """
    if apikey is None:
        apikey = API_KEY

    header = {"Content-Type": "application/json",
              "Authorization": "Bearer " + apikey}

    data = {
        "model": "gpt-3.5-turbo",
        "messages": message_context,
        "stream": True
    }
    print("开始流式请求")
    url = "https://api.openai.com/v1/chat/completions"
    # 请求接收流式数据 动态print
    try:
        
        response = requests.request("POST", url, headers=header, json=data, stream=True)

        def generate():
            # print('nihaohinao')
            # yield "你好"
            stream_content = str()
            one_message = {"role": "assistant", "content": stream_content}
            message_history.append(one_message)
            i = 0
            for line in response.iter_lines():
                # print(str(line))
                line_str = str(line, encoding='utf-8')
                if line_str.startswith("data:"):
                    if line_str.startswith("data: [DONE]"):
                        asyncio.run(save_all_user_dict())
                        break
                    line_json = json.loads(line_str[5:])
                    if 'choices' in line_json:
                        if len(line_json['choices']) > 0:
                            choice = line_json['choices'][0]
                            if 'delta' in choice:
                                delta = choice['delta']
                                if 'role' in delta:
                                    role = delta['role']
                                elif 'content' in delta:
                                    delta_content = delta['content']
                                    i += 1
                                    if i < 40:
                                        print(delta_content, end="")
                                    elif i == 40:
                                        print("......")
                                    one_message['content'] = one_message['content'] + delta_content
                                    yield delta_content

                elif len(line_str.strip()) > 0:
                    print(line_str)
                    yield line_str

    except Exception as e:
        ee = e

        def generate():
            yield "request error:\n" + str(ee)

    return generate


def handle_messages_get_response_stream(message, apikey, message_history, have_chat_context, chat_with_history):
    message_history.append({"role": "user", "content": message})
    asyncio.run(save_all_user_dict())
    message_context = get_message_context(message_history, have_chat_context, chat_with_history)
    generate = get_response_stream_generate_from_ChatGPT_API(message_context, apikey, message_history)
    return generate


def check_session(current_session):
    """
    检查session，如果不存在则创建新的session
    :param current_session: 当前session
    :return: 当前session
    """
    if current_session.get('session_id') is not None:
        print("existing session, session_id:\t", current_session.get('session_id'))
    else:
        current_session['session_id'] = uuid.uuid1()
        print("new session, session_id:\t", current_session.get('session_id'))
    return current_session['session_id']


def check_user_bind(current_session):
    """
    检查用户是否绑定，如果没有绑定则重定向到index
    :param current_session: 当前session
    :return: 当前session
    """
    if current_session.get('user_id') is None:
        return False
    return True


def get_user_info(user_id):
    """
    获取用户信息
    :param user_id: 用户id
    :return: 用户信息
    """
    lock.acquire()
    user_info = all_user_dict.get(user_id)
    lock.release()
    return user_info


# 进入主页
@app.route('/', methods=['GET', 'POST'])
def index():
    """
    主页
    :return: 主页
    """
    check_session(session)
    return render_template('index.html')


@app.route('/loadHistory', methods=['GET', 'POST'])
def load_messages():
    """
    加载聊天记录
    :return: 聊天记录
    """
    check_session(session)
    if session.get('user_id') is None:
        messages_history = [{"role": "assistant", "content": project_info},
                            {"role": "assistant", "content": "#### 当前浏览器会话为首次请求\n"
                                                             "#### 请输入已有用户`id`或创建新的用户`id`。\n"
                                                             "- 已有用户`id`请在输入框中直接输入\n"
                                                             "- 创建新的用户`id`请在输入框中输入`new:xxx`,其中`xxx`为你的自定义id，请牢记\n"
                                                             "- 输入`帮助`以获取帮助提示"}]
    else:
        user_info = get_user_info(session.get('user_id'))
        chat_id = user_info['selected_chat_id']
        messages_history = user_info['chats'][chat_id]['messages_history']
        print(f"用户({session.get('user_id')})加载聊天记录，共{len(messages_history)}条记录")
    return {"code": 0, "data": messages_history, "message": ""}


@app.route('/loadChats', methods=['GET', 'POST'])
def load_chats():
    """
    加载聊天联系人
    :return: 聊天联系人
    """
    check_session(session)
    if not check_user_bind(session):
        chats = []

    else:
        user_info = get_user_info(session.get('user_id'))
        chats = []
        for chat_id, chat_info in user_info['chats'].items():
            chats.append(
                {"id": chat_id, "name": chat_info['name'], "selected": chat_id == user_info['selected_chat_id']})

    return {"code": 0, "data": chats, "message": ""}


def new_chat_dict(user_id, name, send_time):
    return {"chat_with_history": False,
            "have_chat_context": 0,  # 从每次重置聊天模式后开始重置一次之后累计
            "name": name,
            "messages_history": [{"role": "assistant", "content": project_info},
                                 {"role": "system", "content": f"当前对话的用户id为{user_id}"},
                                 {"role": "system", "content": send_time},
                                 {"role": "system", "content": f"你已添加了{name}，现在可以开始聊天了。"},
                                 ]}


def new_user_dict(user_id, send_time):
    chat_id = str(uuid.uuid1())
    user_dict = {"chats": {chat_id: new_chat_dict(user_id, "默认对话", send_time)},
                 "selected_chat_id": chat_id,
                 "default_chat_id": chat_id}

    user_dict['chats'][chat_id]['messages_history'].insert(1, {"role": "assistant",
                                                               "content": "- 创建新的用户id成功，请牢记该id  \n"
                                                                          "- 您可以使用该网站提供的通用apikey进行对话，"
                                                                          "也可以输入 set_apikey:[your_apikey](https://platform.openai.com/account/api-keys) "
                                                                          "来设置用户专属apikey"})
    return user_dict


def get_balance(apikey):
    head = ""
    if apikey is not None:
        head = "###  用户专属api key余额  \n"
    else:
        head = "### 通用api key  \n"
        apikey = API_KEY

    subscription_url = "https://api.openai.com/v1/dashboard/billing/subscription"
    headers = {
        "Authorization": "Bearer " + apikey,
        "Content-Type": "application/json"
    }
    subscription_response = requests.get(subscription_url, headers=headers)
    if subscription_response.status_code == 200:
        data = subscription_response.json()
        total = data.get("hard_limit_usd")
    else:
        return head+subscription_response.text

    # start_date设置为今天日期前99天
    start_date = (datetime.datetime.now() - datetime.timedelta(days=99)).strftime("%Y-%m-%d")
    # end_date设置为今天日期+1
    end_date = (datetime.datetime.now() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    billing_url = f"https://api.openai.com/v1/dashboard/billing/usage?start_date={start_date}&end_date={end_date}"
    billing_response = requests.get(billing_url, headers=headers)
    if billing_response.status_code == 200:
        data = billing_response.json()
        total_usage = data.get("total_usage") / 100
        daily_costs = data.get("daily_costs")
        days = min(5, len(daily_costs))
        recent = f"##### 最近{days}天使用情况  \n"
        for i in range(days):
            cur = daily_costs[-i-1]
            date = datetime.datetime.fromtimestamp(cur.get("timestamp")).strftime("%Y-%m-%d")
            line_items = cur.get("line_items")
            cost = 0
            for item in line_items:
                cost += item.get("cost")
            recent += f"\t{date}\t{cost / 100} \n"
    else:
        return head+billing_response.text

    return head+f"\n#### 总额:\t{total:.4f}  \n" \
                f"#### 已用:\t{total_usage:.4f}  \n" \
                f"#### 剩余:\t{total-total_usage:.4f}  \n" \
                f"\n"+recent


@app.route('/returnMessage', methods=['GET', 'POST'])
def return_message():
    """
    获取用户发送的消息，调用get_chat_response()获取回复，返回回复，用于更新聊天框
    :return:
    """
    check_session(session)
    send_message = request.values.get("send_message").strip()
    send_time = request.values.get("send_time").strip()
    url_redirect = "url_redirect:/"
    if send_message == "帮助":
        return "### 帮助\n" \
               "1. 输入`new:xxx`创建新的用户id\n " \
               "2. 输入`id:your_id`切换到已有用户id，新会话时无需加`id:`进入已有用户\n" \
               "3. 输入`set_apikey:`[your_apikey](https://platform.openai.com/account/api-keys)设置用户专属apikey，`set_apikey:none`可删除专属key\n" \
               "4. 输入`rename_id:xxx`可将当前用户id更改\n" \
               "5. 输入`查余额`可获得余额信息及最近几天使用量\n" \
               "6. 输入`帮助`查看帮助信息"

    if session.get('user_id') is None:  # 如果当前session未绑定用户
        print("当前会话为首次请求，用户输入:\t", send_message)
        if send_message.startswith("new:"):
            user_id = send_message.split(":")[1]
            if user_id in all_user_dict:
                session['user_id'] = user_id
                return url_redirect
            user_dict = new_user_dict(user_id, send_time)
            lock.acquire()
            all_user_dict.put(user_id, user_dict)  # 默认普通对话
            lock.release()
            print("创建新的用户id:\t", user_id)
            session['user_id'] = user_id
            return url_redirect
        else:
            user_id = send_message
            user_info = get_user_info(user_id)
            if user_info is None:
                return "用户id不存在，请重新输入或创建新的用户id"
            else:
                session['user_id'] = user_id
                print("已有用户id:\t", user_id)
                # 重定向到index
                return url_redirect
    else:  # 当存在用户id时
        if send_message.startswith("id:"):
            user_id = send_message.split(":")[1].strip()
            user_info = get_user_info(user_id)
            if user_info is None:
                return "用户id不存在，请重新输入或创建新的用户id"
            else:
                session['user_id'] = user_id
                print("切换到已有用户id:\t", user_id)
                # 重定向到index
                return url_redirect
        elif send_message.startswith("new:"):
            user_id = send_message.split(":")[1]
            if user_id in all_user_dict:
                return "用户id已存在，请重新输入或切换到已有用户id"
            session['user_id'] = user_id
            user_dict = new_user_dict(user_id, send_time)
            lock.acquire()
            all_user_dict.put(user_id, user_dict)
            lock.release()
            print("创建新的用户id:\t", user_id)
            return url_redirect
        elif send_message.startswith("delete:"):  # 删除用户
            user_id = send_message.split(":")[1]
            if user_id != session.get('user_id'):
                return "只能删除当前会话的用户id"
            else:
                lock.acquire()
                all_user_dict.delete(user_id)
                lock.release()
                session['user_id'] = None
                print("删除用户id:\t", user_id)
                # 异步存储all_user_dict
                asyncio.run(save_all_user_dict())
                return url_redirect
        # elif send_message.startswith("set_apikey:"):
        #     apikey = send_message.split(":")[1]
        #     user_info = get_user_info(session.get('user_id'))
        #     user_info['apikey'] = apikey
        #     print("设置用户专属apikey:\t", apikey)
        #     return "设置用户专属apikey成功"
        elif send_message.startswith("rename_id:"):
            new_user_id = send_message.split(":")[1]
            user_info = get_user_info(session.get('user_id'))
            if new_user_id in all_user_dict:
                return "用户id已存在，请重新输入"
            else:
                lock.acquire()
                all_user_dict.delete(session['user_id'])
                all_user_dict.put(new_user_id, user_info)
                lock.release()
                session['user_id'] = new_user_id
                asyncio.run(save_all_user_dict())
                print("修改用户id:\t", new_user_id)
                return f"修改成功,请牢记新的用户id为:{new_user_id}"
        elif send_message == "查余额":
            user_info = get_user_info(session.get('user_id'))
            apikey = user_info.get('apikey')
            return get_balance(apikey)
        else:  # 处理聊天数据
            user_id = session.get('user_id')
            print(f"用户({user_id})发送消息:{send_message}")
            user_info = get_user_info(user_id)
            chat_id = user_info['selected_chat_id']
            messages_history = user_info['chats'][chat_id]['messages_history']
            chat_with_history = user_info['chats'][chat_id]['chat_with_history']
            apikey = user_info.get('apikey')
            if chat_with_history:
                user_info['chats'][chat_id]['have_chat_context'] += 1
            if send_time != "":
                messages_history.append({'role': 'system', "content": send_time})
            if not STREAM_FLAG:
                # content = handle_messages_get_response(send_message, apikey, messages_history,
                #                                        user_info['chats'][chat_id]['have_chat_context'],
                #                                        chat_with_history)
                # content = "可以"
                query = send_message
                for key in API_KEY:
                    try:
                        chain_type_kwargs = {"prompt": PROMPT}
                        chain = RetrievalQA.from_chain_type(llm=OpenAI(model_name="gpt-3.5-turbo",max_tokens=500,temperature=0,openai_api_key=key), chain_type="stuff", retriever=docsearch.as_retriever(), chain_type_kwargs=chain_type_kwargs,verbose=True,return_source_documents=True)
                        content = chain({"query":query})
                        print(content)
                        break
                    except:
                        print('当前key失效，将使用新的key')
                        continue                
                result = content['result']
                a = result.split("。")
                a.remove('')
                rouge = Rouge()
                source = []
                for sub_string in a:
                    tmp_score = []
                    for i in range(len(content["source_documents"])):
                        sub_doc = content["source_documents"][i]
                        rouge_score = rouge.get_scores([' '.join(list(sub_string))], [' '.join(list(sub_doc.page_content))])
                        tmp_score.append(rouge_score[0]["rouge-l"]['f'])
                    source.append(tmp_score.index(max(tmp_score)))

                final_res = ''
                for i in range(len(a)-1):
                    if source[i]==source[i+1]:
                        final_res += (a[i]+',')
                    else:
                        final_res += (a[i]+'。'+'[{}]'.format(str(source[i]+1)))
                final_res += (a[-1]+'。'+ '[{}]'.format(str(source[-1]+1)))

                result = final_res
                result += '\n\n\n参考资料：'
                source = list(set(source))
                for i in source:
                    result += ('\n\n'+ "[{}] ".format(str(i+1)) + str(content["source_documents"][i].metadata))


                print(f"用户({session.get('user_id')})得到的回复消息:{result[:40]}...")
                if chat_with_history:
                    user_info['chats'][chat_id]['have_chat_context'] += 1
                # 异步存储all_user_dict
                asyncio.run(save_all_user_dict())
                return result
            else:
                generate = handle_messages_get_response_stream(send_message, apikey, messages_history,
                                                               user_info['chats'][chat_id]['have_chat_context'],
                                                               chat_with_history)

                if chat_with_history:
                    user_info['chats'][chat_id]['have_chat_context'] += 1

                return app.response_class(generate(), mimetype='application/json')


async def save_all_user_dict():
    """
    异步存储all_user_dict
    :return:
    """
    await asyncio.sleep(0)
    lock.acquire()
    with open(USER_DICT_FILE, "wb") as f:
        pickle.dump(all_user_dict, f)
    # print("all_user_dict.pkl存储成功")
    lock.release()


@app.route('/getMode', methods=['GET'])
def get_mode():
    """
    获取当前对话模式
    :return:
    """
    check_session(session)
    if not check_user_bind(session):
        return "normal"
    user_info = get_user_info(session.get('user_id'))
    chat_id = user_info['selected_chat_id']
    chat_with_history = user_info['chats'][chat_id]['chat_with_history']
    if chat_with_history:
        return {"mode": "continuous"}
    else:
        return {"mode": "normal"}


@app.route('/changeMode/<status>', methods=['GET'])
def change_mode(status):
    """
    切换对话模式
    :return:
    """
    check_session(session)
    if not check_user_bind(session):
        return {"code": -1, "msg": "请先创建或输入已有用户id"}
    user_info = get_user_info(session.get('user_id'))
    chat_id = user_info['selected_chat_id']
    if status == "normal":
        user_info['chats'][chat_id]['chat_with_history'] = False
        print("开启普通对话")
        message = {"role": "system", "content": "切换至普通对话"}
    else:
        user_info['chats'][chat_id]['chat_with_history'] = True
        user_info['chats'][chat_id]['have_chat_context'] = 0
        print("开启连续对话")
        message = {"role": "system", "content": "切换至连续对话"}
    user_info['chats'][chat_id]['messages_history'].append(message)
    return {"code": 200, "data": message}


@app.route('/selectChat', methods=['GET'])
def select_chat():
    """
    选择聊天对象
    :return:
    """
    chat_id = request.args.get("id")
    check_session(session)
    if not check_user_bind(session):
        return {"code": -1, "msg": "请先创建或输入已有用户id"}
    user_id = session.get('user_id')
    user_info = get_user_info(user_id)
    user_info['selected_chat_id'] = chat_id
    return {"code": 200, "msg": "选择聊天对象成功"}


@app.route('/newChat', methods=['GET'])
def new_chat():
    """
    新建聊天对象
    :return:
    """
    name = request.args.get("name")
    time = request.args.get("time")
    check_session(session)
    if not check_user_bind(session):
        return {"code": -1, "msg": "请先创建或输入已有用户id"}
    user_id = session.get('user_id')
    user_info = get_user_info(user_id)
    new_chat_id = str(uuid.uuid1())
    user_info['selected_chat_id'] = new_chat_id
    user_info['chats'][new_chat_id] = new_chat_dict(user_id, name, time)
    print("新建聊天对象")
    return {"code": 200, "data": {"name": name, "id": new_chat_id, "selected": True}}

@app.route('/fileUpload', methods=['GET','POST'])
def fileUpload():
    """
    上传文件
    :return:
    """
    if request.method == 'POST':
        # input标签中的name的属性值
        f = request.files['file']
        # 拼接地址，上传地址，f.filename：直接获取文件名
        file = f.save(os.path.join(app.config['UPLOAD_FOLDER'], f.filename))
        # 输出上传的文件名
        print(request.files, f.filename)
        return file
    else:
        return " "

@app.route('/deleteHistory', methods=['GET'])
def delete_history():
    """
    清空上下文
    :return:
    """
    check_session(session)
    if not check_user_bind(session):
        print("请先创建或输入已有用户id")
        return {"code": -1, "msg": "请先创建或输入已有用户id"}
    user_info = get_user_info(session.get('user_id'))
    chat_id = user_info['selected_chat_id']
    default_chat_id = user_info['default_chat_id']
    if default_chat_id == chat_id:
        print("清空历史记录")
        user_info["chats"][chat_id]['messages_history'] = user_info["chats"][chat_id]['messages_history'][:5]
    else:
        print("删除聊天对话")
        del user_info["chats"][chat_id]
    user_info['selected_chat_id'] = default_chat_id
    return "2"

def check_load_pickle():
    global all_user_dict

    if os.path.exists(USER_DICT_FILE):
        with open(USER_DICT_FILE, "rb") as pickle_file:
            all_user_dict = pickle.load(pickle_file)
            all_user_dict.change_capacity(USER_SAVE_MAX)
        print(f"已加载上次存储的用户上下文，共有{len(all_user_dict)}用户, 分别是")
        for i, user_id in enumerate(list(all_user_dict.keys())):
            print(f"{i} 用户id:{user_id}\t对话统计:\t", end="")
            user_info = all_user_dict.get(user_id)
            for chat_id in user_info['chats'].keys():
                print(f"{user_info['chats'][chat_id]['name']}[{len(user_info['chats'][chat_id]['messages_history'])}] ",
                      end="")
            print()
    elif os.path.exists("all_user_dict.pkl"):  # 适配当出现这个时
        print('检测到v1版本的上下文，将转换为v2版本')
        with open("all_user_dict.pkl", "rb") as pickle_file:
            all_user_dict = pickle.load(pickle_file)
            all_user_dict.change_capacity(USER_SAVE_MAX)
        print("共有用户", len(all_user_dict), "个")
        for user_id in list(all_user_dict.keys()):
            user_info: dict = all_user_dict.get(user_id)
            if "messages_history" in user_info:
                user_dict = new_user_dict(user_id, "")
                chat_id = user_dict['selected_chat_id']
                user_dict['chats'][chat_id]['messages_history'] = user_info['messages_history']
                user_dict['chats'][chat_id]['chat_with_history'] = user_info['chat_with_history']
                user_dict['chats'][chat_id]['have_chat_context'] = user_info['have_chat_context']
                all_user_dict.put(user_id, user_dict)  # 更新
        asyncio.run(save_all_user_dict())
    else:
        with open(USER_DICT_FILE, "wb") as pickle_file:
            pickle.dump(all_user_dict, pickle_file)
        print("未检测到上次存储的用户上下文，已创建新的用户上下文")

    # 判断all_user_dict是否为None且时LRUCache的对象
    if all_user_dict is None or not isinstance(all_user_dict, LRUCache):
        print("all_user_dict为空或不是LRUCache对象，已创建新的LRUCache对象")
        all_user_dict = LRUCache(USER_SAVE_MAX)


def test_question():
    # open the file in context/questions json_data/qestions and wx_json/questions
    contextPath=['./context','./wx_json','./json_data']
    for path in contextPath:
        for filename in os.listdir(path+'/questions'):
            print('start ',filename)
            # open a file to read and write
            with open(path+'/questions/'+filename,'r',encoding='utf-8') as f:
                context=f.read()
                if context=='':
                    continue
                context=json.loads(context)
                # if context has key integrity, then skip
                if 'integrity' in context:
                    continue
                title=context['title']
                questions=context['questions']
                
                # test if retrieve the right document
                for question in questions:
                    integrity=False
                    if len(question['question'])>500:
                        continue
                    print(question)
                    # wait for 3 sec
                    time.sleep(23)
                    result=chain({'query':question['question']})
                    print(result)
                    for doc in result['source_documents']:
                        if doc.metadata['title']==title:
                            integrity=True
                            break
                    question['integrity']=integrity
                    question['result']=result['result']
                    question['response']=result
                
                context['questions']=questions
                f.close()
            json.dump(context,open(path+'/questions/'+filename,'w',encoding='utf-8'),ensure_ascii=False,indent=4)
            print('finish ',filename)

if __name__ == '__main__':
    print("持久化存储文件路径为:", os.path.join(os.getcwd(), USER_DICT_FILE))
    all_user_dict = LRUCache(USER_SAVE_MAX)
    check_load_pickle()

    contextPath='./context'
    jsonPath='./json_data'
    logPath='./log'

    if len(API_KEY) == 0:
        # 退出程序
        print("请在openai官网注册账号，获取api_key填写至程序内或命令行参数中")
        exit()
    
    persist_directory = 'db'
    
    embeddings = OpenAIEmbeddings(openai_api_key=API_KEY[0])

    #先基于seperators[0]划分，如果两个seperators[0]之间的距离大于chunk_size，使用seperators[1]继续划分......
    # text_splitter = RecursiveCharacterTextSplitter( separators = ["\n \n","。",",",],chunk_size=500, chunk_overlap=0)
    #基于seperator划分，如果两个seperator之间的距离大于chunk_size,该chunk的size会大于chunk_size
    text_splitter = CharacterTextSplitter( separator = "。",chunk_size=300, chunk_overlap=0)
    doc_splitter = CharacterTextSplitter(separator = "。\n\n",chunk_size=150, chunk_overlap=0)
    docsearch=None

    for filename in os.listdir(contextPath):
        # TODO　update into from_document after update creep function
    # deal with txt
        if filename.endswith('.txt'):
            continue
            with open(os.path.join(contextPath, filename), 'r', encoding='utf-8') as f:
                # if file is empty, skip it
                if os.stat(os.path.join(contextPath, filename)).st_size == 0:
                    continue
                print("正在向量化文件：", filename)
                file_split_docs = text_splitter.split_text(f.read())
                # if docsearch does not exists, create a new one
                if len(file_split_docs) > 0:
                    if docsearch is None:
                        print("创建新的docsearch",file_split_docs)
                        docsearch = Chroma.from_texts(file_split_docs, embeddings)
                    # else add the texts to the existing one
                    else:
                        docsearch.add_texts(file_split_docs)
                f.close()
    # deal with pdf   
        elif filename.endswith('.pdf'):
            loader = PyPDFLoader(os.path.join(contextPath, filename))
            pages = loader.load()
            print("pdf loading" ,pages)
            split_docs = text_splitter.split_documents(pages)
            print("pdf split",split_docs)

            if len(split_docs) > 0:
                if docsearch is None:
                    docsearch=Chroma.from_documents(split_docs,embeddings)
                else:
                    docsearch.add_documents(split_docs)
    # deal with doc   
        elif filename.endswith('.docx')or filename.endswith('.doc'):
            loader = Docx2txtLoader(os.path.join(contextPath, filename))
            pages = loader.load()
            print("doc loading" ,pages)
            split_docs = doc_splitter.split_documents(pages)
            print("doc split",split_docs)

            if len(split_docs) > 0:
                if docsearch is None:
                    # docsearch=Chroma.from_documents(split_docs,embeddings)
                    pass
                else:
                    docsearch.add_documents(split_docs)
    # deal with json
    jsloader = json_loader()
    json_data = jsloader.load()

    if len(split_docs) > 0:
        if docsearch is None:
            # docsearch=Chroma.from_documents(json_data,embeddings)
            pass
        else:
            docsearch.add_documents(json_data)

    # deal with wx_json
    jsloader = wx_loader()
    json_data = jsloader.load()
    split_docs = text_splitter.split_documents(json_data)

    if len(split_docs) > 0:
        if docsearch is None:

            docsearch=Chroma.from_documents(split_docs,embeddings)
        else:
            docsearch.add_documents(split_docs)

    print("完成向量化")

    # docsearch.persist()
    # print(len(docsearch))
    # chain_type_kwargs = {"prompt": PROMPT}
    # chain = RetrievalQA.from_chain_type(llm=OpenAI(model_name="gpt-3.5-turbo",max_tokens=500,temperature=0,openai_api_key=API_KEY), chain_type="stuff", retriever=docsearch.as_retriever(), chain_type_kwargs=chain_type_kwargs,verbose=True,return_source_documents=True)
    # print(chain({'query': "离职人员可以自己缴纳公积金吗?"}))
    app.run(host="0.0.0.0", port=PORT, debug=False)