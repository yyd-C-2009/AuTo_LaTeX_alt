from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Button, Static
from textual.containers import HorizontalGroup

class CounterApp(App):
    BINDINGS = [("d", "toggle_dark", "切换主题")]

    def compose(self) -> ComposeResult:
        yield Header()
        # 使用 HorizontalGroup 让按钮水平排列
        with HorizontalGroup():
            yield Button("点击我 +1", id="inc")
            yield Button("重置", id="reset")
        yield Static("计数: 0", id="count_display") # 用于显示数字
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed):
        """处理所有按钮点击事件"""
        count_widget = self.query_one("#count_display", Static)
        current_text = count_widget.renderable # 获取当前文本
        current_count = int(current_text.split(": ")[1]) # 解析数字

        if event.button.id == "inc":
            new_count = current_count + 1
        elif event.button.id == "reset":
            new_count = 0

        count_widget.update(f"计数: {new_count}") # 更新显示

if __name__ == "__main__":
    CounterApp().run()