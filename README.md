# DeepSeek 余额查询

AstrBot 插件：让机器人自主查询 DeepSeek 账户余额；余额低于设定阈值时，用当前人格生成提醒并主动推送。

## 功能

| 功能 | 说明 |
|:---|:---|
| LLM 工具 | 注册 `check_deepseek_balance`，机器人可自主查询余额（用户问"还剩多少钱"时自动调用） |
| 低额提醒 | 后台按间隔检查，余额低于阈值时用人格口吻生成提醒，推送到白名单会话 |
| 手动命令 | `/余额` 查询，`/余额 测试` 预览人格化提醒文案 |
| 多账号 | 自动识别所有已配置的 DeepSeek 账号，可指定查询 |
| 零配置密钥 | 直接复用 AstrBot 的 `provider_sources`，无需重复填写 API Key |

## 命令

```
/余额                 查询全部 DeepSeek 账号
/余额 deepseek_1      查询指定账号
/余额 测试            预览人格化提醒文案与推送白名单
```

## 配置

| 配置项 | 默认 | 说明 |
|:---|:---|:---|
| `enabled` | `true` | 是否启用低额提醒 |
| `threshold` | `10.0` | 余额警戒线（元），低于此值触发 |
| `target_sessions` | `[]` | 推送白名单，可填群号或完整会话 ID；留空则只发管理员私聊 |
| `also_notify_admin` | `true` | 除白名单外，额外私聊提醒管理员 |
| `check_interval_hours` | `6.0` | 检查间隔（小时） |
| `remind_cooldown_hours` | `24.0` | 提醒冷却（小时），避免刷屏 |
| `use_persona` | `true` | 用人格生成提醒文案 |
| `persona_name` | `""` | 指定人格，留空跟随 AstrBot 默认人格 |
| `remind_instruction` | 见面板 | 提醒生成指令，只描述"这是主动发起的一条消息" |
| `chat_provider_id` | `""` | 生成提醒所用模型，留空用 `default_provider_id` |
| `request_timeout` | `20.0` | 余额接口超时（秒） |

### 推送目标写法

```
2156064175                          # 纯群号，自动按群聊解析
default:GroupMessage:2156064175     # 完整会话 ID
default:FriendMessage:123456789     # 指定私聊
```

## 原理

- 余额来自 DeepSeek 官方接口 `GET https://api.deepseek.com/user/balance`
- 后台循环先等 60 秒（等平台适配器连接），此后每 `check_interval_hours` 检查一次
- 仅在实际需要提醒时才调用 LLM 生成文案；提醒成功后写入冷却时间
- 全部目标推送失败时不写入冷却，下一轮重试

## 安装

```bash
cd /path/to/AstrBot/data/plugins
git clone https://github.com/misha-233/astrbot_plugin_ds_balance.git
```

重启 AstrBot 后在插件面板配置。

## 依赖

无额外依赖（使用 AstrBot 自带的 `httpx`）。

## License

MIT
