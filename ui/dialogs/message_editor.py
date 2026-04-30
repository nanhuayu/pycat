"""
Message editor dialog for editing message content and images - Chinese UI
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTextEdit,
    QPushButton, QLabel, QFileDialog, QFrame,
    QScrollArea, QWidget
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
import base64
import os
from typing import List

from models.conversation import Message
from ui.utils.image_loader import load_pixmap


class ImageItem(QFrame):
    def __init__(self, image_data: str, parent=None):
        super().__init__(parent)
        self.image_data = image_data
        self._setup_ui()
    
    def _setup_ui(self):
        self.setFixedSize(90, 100)
        self.setObjectName("image_edit_item")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)
        
        self.image_label = QLabel()
        self._load_image()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.image_label)
        
        delete_btn = QPushButton("删除")
        delete_btn.setObjectName("image_edit_delete_btn")
        delete_btn.clicked.connect(lambda: self.deleteLater())
        layout.addWidget(delete_btn)
    
    def _load_image(self):
        try:
            pixmap = load_pixmap(self.image_data)
            
            if not pixmap.isNull():
                scaled = pixmap.scaled(70, 50, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
                self.image_label.setPixmap(scaled)
            else:
                self.image_label.setText("🖼️")
        except Exception:
            self.image_label.setText("⚠️")


class MessageEditorDialog(QDialog):
    def __init__(self, message: Message, parent=None):
        super().__init__(parent)
        self.message = message
        self._setup_ui()
    
    def _setup_ui(self):
        self.setWindowTitle("编辑消息")
        self.setMinimumWidth(450)
        self.setMinimumHeight(350)
        
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        
        role_text = "你的" if self.message.role == 'user' else "助手的"
        role_label = QLabel(f"编辑{role_text}消息")
        role_label.setObjectName("editor_role_label")
        layout.addWidget(role_label)
        
        layout.addWidget(QLabel("消息内容:"))
        
        self.content_edit = QTextEdit()
        self.content_edit.setPlainText(self.message.content)
        self.content_edit.setMinimumHeight(150)
        self.content_edit.setObjectName("editor_content")
        layout.addWidget(self.content_edit)
        
        layout.addWidget(QLabel("附加图片:"))
        
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setMaximumHeight(120)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        self.images_container = QWidget()
        self.images_layout = QHBoxLayout(self.images_container)
        self.images_layout.setContentsMargins(4, 4, 4, 4)
        self.images_layout.setSpacing(6)
        
        for image_data in self.message.images:
            self._add_image_item(image_data)
        
        self.images_layout.addStretch()
        scroll_area.setWidget(self.images_container)
        layout.addWidget(scroll_area)
        
        add_image_btn = QPushButton("+ 添加图片")
        add_image_btn.clicked.connect(self._add_image)
        layout.addWidget(add_image_btn)
        
        layout.addStretch()
        
        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        
        save_btn = QPushButton("保存修改")
        save_btn.setProperty("primary", True)
        save_btn.clicked.connect(self.accept)
        btn_layout.addWidget(save_btn)
        
        layout.addLayout(btn_layout)
    
    def _add_image_item(self, image_data: str):
        item = ImageItem(image_data)
        self.images_layout.insertWidget(self.images_layout.count() - 1, item)
    
    def _add_image(self):
        file_paths, _ = QFileDialog.getOpenFileNames(
            self, '添加图片', '',
            '图片 (*.png *.jpg *.jpeg *.gif *.webp);;所有文件 (*)'
        )
        
        for file_path in file_paths:
            try:
                with open(file_path, 'rb') as f:
                    data = base64.b64encode(f.read()).decode('utf-8')
                    ext = os.path.splitext(file_path)[1].lower()
                    mime = {'.png': 'image/png', '.jpg': 'image/jpeg',
                            '.jpeg': 'image/jpeg', '.gif': 'image/gif',
                            '.webp': 'image/webp'}.get(ext, 'image/png')
                    image_data = f"data:{mime};base64,{data}"
                    self._add_image_item(image_data)
            except Exception as e:
                print(f"Error adding image: {e}")
    
    def get_edited_content(self) -> str:
        return self.content_edit.toPlainText()
    
    def get_edited_images(self) -> List[str]:
        images = []
        for i in range(self.images_layout.count()):
            item = self.images_layout.itemAt(i)
            widget = item.widget()
            if isinstance(widget, ImageItem):
                images.append(widget.image_data)
        return images
