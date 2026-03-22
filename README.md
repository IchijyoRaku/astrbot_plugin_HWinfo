# astrbot_plugin_HWinfo

用于 [`AstrBot`](https://docs.astrbot.app/) 的硬件信息快速查询与性能比较插件。

## 功能

- CPU 型号模糊搜索，指令示例：[`cpu 9700x`]
- GPU 型号模糊搜索，指令示例：[`gpu 5070ti`]or[`gpu 5070m`]
- 笔电/台式显卡性能对比，支持类似“笔电的5070相当于台式什么显卡”这类问题(只是简单的ts分数范围匹配，仅供参考)
- 发送 [`显卡天梯图`]

## credit

- 数据源：[`topcpu`](https://www.topcpu.net/)
- 显卡天梯图源：[`百度贴吧 @长安`](https://tieba.baidu.com/home/main?id=tb.1.682a7177.ah_UG7GvcjrymgEoDd8hkQ%3Ft%3D1774060145&fr=pb)
- 参考：[`astrbot_plugin_hardwareinfo`](https://github.com/wuxinTLH/astrbot_plugin_hardwareinfo)
