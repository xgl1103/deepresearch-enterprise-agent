from agent.base_agent import *
import os

os.environ['APP_TOKEN'] = 'test-app-token'
os.environ['LLM_BASE_URL'] = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
os.environ['APP_ID'] = 'test-app-id'
os.environ['GEMINI_API_KEY'] = 'test-gemini-api-key'

if __name__ == '__main__':
    agent = Agent()
    response = agent("中国的首都是那里？")
