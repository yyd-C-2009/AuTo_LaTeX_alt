'''全局运行配置: 模型端点与输出目录。所有模块共用, 不再散落在启动脚本里。'''

MODEL = "deepseek-v4-flash"
BASE_URL = "https://api.deepseek.com" # https://api.llm.ustc.edu.cn/v1 or https://api.deepseek.com
OUT_DIR = "latex_output"
TIKZ_DIR = "tikz_output"
KEY_ID = 'DS_API_KEY'                   #DS_API_KEY OR DSH_OPENAI_KEY
