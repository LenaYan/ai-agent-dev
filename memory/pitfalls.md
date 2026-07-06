# 踩坑笔记 (pitfalls)

记录踩过的坑和解法，避免重复。每条：现象 · 原因 · 解法 · 日期。

格式：
```
## <一句话现象>  (YYYY-MM-DD)
- 原因：<根因>
- 解法：<怎么解决 / 规避>
- 备注：<可选，如版本相关>
```

---

<!-- 示例：
## LangGraph 状态在节点间没有累加  (YYYY-MM-DD)
- 原因：state schema 用了普通赋值而非 reducer，节点返回覆盖了旧值。
- 解法：对需累加的字段用 Annotated[list, add] 之类的 reducer。
- 备注：LangGraph 0.x，API 可能变化。
-->
