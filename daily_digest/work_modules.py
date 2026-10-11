"""The user's six research modules, shared by model instructions and rendering."""

WORK_MODULES = {
    1: ("机器人本体与硬件", "机械结构、腱驱动、执行器、传感器与通信；固件和SDK、装配、标定、限位与停机链路。"),
    2: ("建模、辨识与仿真", "运动学、动力学、传动与接触模型；摩擦、滞回与延迟辨识，MuJoCo仿真建立与误差验证。"),
    3: ("运动规划与控制", "坐标变换、正逆运动学、雅可比、轨迹规划与反馈控制；按接口条件研究力、阻抗与鲁棒控制。"),
    4: ("感知与状态估计", "本体状态、物体姿态与接触估计；视觉和实际传感反馈、部署可观测量与估计误差。"),
    5: ("灵巧操作与策略学习", "围绕抓取与手内操作开展RL/IL，PPO、BC、IL初始化与RL微调，策略泛化与真机验证。"),
    6: ("高层任务与技能规划", "多技能组合、长时序任务与臂手协同，多技能衔接与完整任务的系统验证。"),
}


def work_module_name(module_id: object) -> str:
    """Require one integer ID; booleans and model-authored names are not IDs."""
    if type(module_id) is not int or module_id not in WORK_MODULES:
        raise ValueError("Invalid work module: expected one integer from 1 to 6.")
    return WORK_MODULES[module_id][0]
