
<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_portrayal?name=astrbot_plugin_portrayal&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_portrayal

_✨ 人物画像插件 ✨_  

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/作者-Zhalslar-blue)](https://github.com/Zhalslar)

</div>

> 注意：自本插件v1.1.5开始，已移除插件内置的t2i, 输入内容将直接以长文本形式输出，如果有转图片的需求，推荐使用：[输出增强插件](https://github.com/Zhalslar/astrbot_plugin_outputpro)，插件提供了精美的t2i功能

## 💡 介绍

根据群友的聊天记录，调用llm分析群友的性格画像

## 📦 安装

- 可以直接在astrbot的插件市场搜索astrbot_plugin_portrayal，点击安装即可  

- 或者可以直接克隆源码到插件文件夹：

```bash
# 克隆仓库到插件目录
cd /AstrBot/data/plugins
git clone https://github.com/Zhalslar/astrbot_plugin_portrayal

# 控制台重启AstrBot
```

## ⚙️ 配置

请在astrbot面板配置，插件管理 -> astrbot_plugin_portrayal -> 操作 -> 插件配置

## ⌨️ 使用说明

插件的命令分两类：

- **内置命令**：通过 `@filter.command` 注册，固定不可改，会自动出现在 WebUI 的插件指令列表中
- **提示词命令**：在 `builtin_prompts.yaml` / 插件配置「提示词配置」里定义，可自行增删；它们由通用监听器响应，WebUI 不会单独列出

## ⌨️ 指令表

### 提示词命令（可在插件配置中自定义增删）

| 指令 | 说明 |
|:---:|:---:|
| `画像 @群友 <轮数>` | 综合性格画像（含优点/缺点/相处建议） |
| `正画像 @群友 <轮数>` | 偏优点向的画像 |
| `负画像 @群友 <轮数>` | 偏缺点向的画像（理性审判风格） |
| `克隆人格 @群友 <轮数>` | 生成可用于「切换人格」的 system prompt，保存为该群友的克隆模板 |

> 轮数可省略，默认走插件配置里的 `default_query_rounds`。

### 内置命令

| 指令 | 权限 | 说明 |
|:---:|:---:|:---:|
| `查看画像 @群友` | 所有人 | 查看本地已生成的画像 |
| `切换人格 @群友` | Admin | 把当前会话切到该群友的克隆人格，并同步 bot 的 QQ 昵称/头像。**前置：需先执行「克隆人格 @群友」生成模板** |
| `恢复人格` | Admin | 一键还原：恢复默认人格、清空当前对话历史、还原 bot 昵称/头像 |

## 效果图

![download](https://github.com/user-attachments/assets/988e7cc1-92d1-48c9-8d95-cf83e802bfc9)

## 👥 贡献指南

- 🌟 Star 这个项目！（点右上角的星星，感谢支持！）
- 🐛 提交 Issue 报告问题
- 💡 提出新功能建议
- 🔧 提交 Pull Request 改进代码

## 📌 注意事项

- 想第一时间得到反馈的可以来作者的插件反馈群（QQ群）：460973561（不点star不给进）
