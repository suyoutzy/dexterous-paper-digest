"""Small model-output fixture with explicitly sourced experimental evidence."""

def v4_row(identifier="arxiv:2609.00001"):
    return {
        "content_version": 4, "paper_id": identifier, "title_zh": "灵巧手标定与控制",
        "work_modules": [1, 3],
        "research": {"problem": "固定结构与姿态映射分开设计，限制手指对齐。",
            "idea": "联合优化机构参数与姿态映射，减少结构造成的误差。",
            "implementation": ["输入人手姿态，通过运动学模型调整连杆和映射参数。", "施加装配约束，输出可制造结构与控制映射。"]},
        "experiments": {"setup": "在相同任务与控制条件下比较两种结构。",
            "results": [{"text": "误差为3.9 mm，基线为12.8 mm。",
                "quote": "The error was 3.9 mm versus 12.8 mm."}],
            "limitations": [{"kind": "analysis", "text": "给定材料不足以证明跨任务泛化。", "quote": ""}]},
        "terms": [{"english": "Inverse Kinematics (IK)", "chinese": "逆运动学",
            "meaning": "由目标位置求关节配置。", "role": "将人手关键点转换为机器人关节目标。"},
            {"english": "Root Mean Square Error (RMSE)", "chinese": "均方根误差",
            "meaning": "衡量预测与目标之间的平均偏差。", "role": "比较机构对齐的几何误差。"}],
        "insight": "机构和控制接口可以联合设计，适用于灵巧末端优化。",
        "reading": {"level": "选择性阅读", "focus": "优先查看协同优化方法与对照实验。"},
    }


def evidence_text():
    return "The error was 3.9 mm versus 12.8 mm. Testing was limited to one task."
