# PyCat Global Memory

> 这是 **PyCat 全局 Memory** 的入口文件。
>
> 与 Session Memory（当前会话）和 Workspace Memory（当前工作区 `.pycat/memory/`）不同，
> 全局 Memory 存储在 `~/.PyCat/memory/` 下，**跨所有工作区生效**，用于保存你的长期偏好、角色信息和跨项目通用知识。

## 文件命名规范

本目录下的每个 `.md` 或 `.txt` 文件都是一个独立的 memory topic。推荐命名格式：

```
memory__<topic>.md
```

例如：
- `memory__user.md` — 你的角色、目标、职责和知识背景
- `memory__feedback.md` — 你给予 PyCat 的行为指导与反馈
- `memory__reference.md` — 外部系统引用（如文档链接、工具地址）
- `memory__<custom>.md` — 其他自定义主题

## Memory 写入原则

1. **有用**：未来任务可能再次用到
2. **稳定**：不是临时状态或很快过期的信息
3. **可解释**：知道为什么写入、来源是什么
4. **可删除**：你能查看和删除任何 memory
5. **有权限**：敏感信息、API key、密码不能自动写入

## 不应写入的内容

- API key、cookie、token、密码
- 临时状态或单次使用的信息
- 未验证的猜测
- 大段工具输出原文
- 与未来任务无关的闲聊
- 可从代码或 git 历史直接推导的信息

## 当前 Memory 文件

<!-- 在此列出你创建的 memory 文件，作为索引 -->

- [memory__user.md](memory__user.md)
- [memory__feedback.md](memory__feedback.md)
