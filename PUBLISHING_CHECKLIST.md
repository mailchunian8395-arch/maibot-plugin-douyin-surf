# 发布检查清单

首个公开版本为 `1.0.0`。请使用公开仓库 `https://github.com/mailchunian8395-arch/maibot-plugin-douyin-surf` 后，按以下顺序完成发布。

## 发布信息确认

- `_manifest.json` 中的 ID 固定为 `chunian.maibot-plugin-douyin-surf`；
- 仓库、文档、主页和 Issue 链接均指向 `mailchunian8395-arch/maibot-plugin-douyin-surf`；
- 首次公开版本固定为 `1.0.0`。

如 GitHub 用户名或仓库名与上述地址不一致，必须在上传前统一修改 manifest、README 和本清单中的链接。

## 发布前命令

在 MaiBot 根目录执行：

```powershell
py -3.14 plugins\maibot-plugin-douyin-surf\scripts\validate_release.py
py -3.14 -m py_compile plugins\maibot-plugin-douyin-surf\plugin.py plugins\maibot-plugin-douyin-surf\config_model.py
```

然后确认默认配置仍符合安全原则：

- `plugin.enabled = false`；
- `surf.enabled = false`；
- `sharing.enabled = false`；
- `sharing.stream_configs = []`；
- 不包含外部媒体转发接口配置；通用版只使用插件自身的直接发送能力。

## 插件中心提交

将仓库设为公开后，在 MaiBot `plugin-repo` 创建 **Add Plugin / 添加插件** Issue，填写与 manifest 完全一致的插件 ID 与仓库 HTTPS 地址。CI 验证失败时，按 Issue 的提示修复后评论 `/recheck`。

## 隐私与文件检查

提交前不得包含以下内容：

- `data/`、`browser-profile/`、SQLite 数据库、浏览器 Cookie、视频缓存和日志；
- 任意真实 QQ 群号、私聊 ID、访问令牌或账号密码；
- 仅在作者本地存在的跨插件 API 名称或绝对路径。
