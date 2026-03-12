
<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_portrayal?name=astrbot_plugin_portrayal&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_portrayal

_✨ 人物画像插件 ✨_  

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/作者-Zhalslar-blue)](https://github.com/Zhalslar)

</div>

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

## ⌨️ 指令表

|     指令      |                    说明                    |
|:-------------:|:-----------------------------------------------:|
| 画像@群友 [轮数] [偏好] | 分析这位群友的性格画像，并可按偏好追加人物搜图 |
| 找画像/找人物@群友 [偏好] | 根据已经生成好的画像，匹配一个相似的公众人物或角色，并输出搜到的图片 |
| 查看画像@群友 | 查看本地已保存的画像内容 |
| 切换人格@群友 | 将当前对话切换为该群友的克隆人格 |
| 画像提示词 <命令/留空> | 查看某套提示词的内容， 不指定命令则默认查看所有提示词                    |

### 命令示例

```text
画像 @小明
画像 @小明 40
画像 @小明 30 二次元
找画像 @小明
找人物 @小明 film_tv
找画像 @小明 历史人物
查看画像 @小明
切换人格 @小明
```

## 搜图功能说明

- 新增 `找画像 @群友` 指令，会读取本地画像文本，先用当前 LLM 推断最像的人物，再调用搜图 API 拉取图片。
- 执行 `画像 @群友` 成功后，如果已经在配置中启用搜图功能，也会在原有画像结果后继续输出匹配人物和图片。
- 当前默认接入 [SerpApi Google Images Search](https://serpapi.com/google-images-api)，需要在插件配置中填写 `image_search.api_key`。

### 配置填写示例

在 AstrBot 插件配置中找到 `image_search` 配置组，按下面方式填写：

```json
{
  "enabled": true,
  "api_key": "你的 SerpApi Key",
  "endpoint": "https://serpapi.com/search.json",
  "engine": "google_images",
  "preference": "auto",
  "result_limit": 5,
  "request_timeout_sec": 20,
  "language": "zh-cn",
  "country": "cn"
}
```

### `image_search.api_key` 获取方式

1. 打开 [SerpApi 官网](https://serpapi.com/) 注册并登录。
2. 进入 [API Key 管理页面](https://serpapi.com/manage-api-key)。
3. 复制页面中的 API Key。
4. 将这个值填入插件配置里的 `image_search.api_key`。

### 注意事项

- `image_search.preference` 用于控制匹配偏好，可选值：
  `auto` 自动、`anime` 二次元、`film_tv` 影视作品、`historical` 历史人物、`real_person` 现实人物。
- 命令里也可以临时指定偏好，例如：
  `找画像 @群友 二次元`
  `找画像 @群友 real_person`
  `画像 @群友 30 历史人物`
- 这里填写的是 `SerpApi API Key`，不是 Google Key，也不是 OpenAI Key。
- 如果只升级插件但没安装新依赖，请先安装 `httpx`。
- 若 `image_search.enabled=true` 但 `api_key` 为空，插件不会执行搜图。

## 版本升级说明

### v1.2.0

- 本版本新增搜图能力、偏好设置和命令临时偏好参数。
- 详细变更请查看仓库中的 `CHANGELOG.md`。

### 升级步骤

1. 更新插件代码到 `v1.2.0`。
2. 重新安装插件依赖，确保 `httpx` 已安装。
3. 在 AstrBot 插件配置中检查 `image_search` 配置组。
4. 如需启用搜图功能，填写 `image_search.api_key` 并开启 `image_search.enabled`。
5. 重新加载或重启 AstrBot。

## 效果图

![download](https://github.com/user-attachments/assets/988e7cc1-92d1-48c9-8d95-cf83e802bfc9)

## 👥 贡献指南

- 🌟 Star 这个项目！（点右上角的星星，感谢支持！）
- 🐛 提交 Issue 报告问题
- 💡 提出新功能建议
- 🔧 提交 Pull Request 改进代码

## 📌 注意事项

- 想第一时间得到反馈的可以来作者的插件反馈群（QQ群）：460973561（不点star不给进）
