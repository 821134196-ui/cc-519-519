"""灯箱广告亮灯监管系统。

模块:
  db       SQLite 存储与建表
  clock    可替换的虚拟时钟（测试用）
  schedule 亮灯计划时段判断
  signal_  模拟亮灯信号适配器
  photos   本地照片写入
  engine   巡检分类、故障归并、派单、复验
  makeup   补播核算（按合同版本）
  seed     预置"半亮"样例
  web      HTTP 界面
"""
