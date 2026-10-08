"""所有测试共用的设置：单元测试不连 LangSmith。必须在 app.config 被导入之前设好。"""
import os

os.environ["LANGSMITH_TRACING"] = "false"
