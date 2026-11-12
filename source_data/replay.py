"""按声明顺序返回不解释的源响应，用于离线复演只读接口。"""
import copy

READ_OPERATIONS = {'ReadHolding': 3, 'ReadInput': 4}
WRITE_FUNCTIONS = {5, 6, 15, 16, 22, 23}
READ_OPERATIONS.update({'Read': 3, 'ReadInputRegisters': 4})


class Replay:
    """虚拟寄存器源：只接受功能 03/04，其余调用立即拒绝且不消耗步骤。"""

    def __init__(self, steps):
        self.steps = copy.deepcopy(steps)
        self.position = 0
        self.calls = []

    def call(self, operation, arguments):
        if operation not in READ_OPERATIONS:
            raise ValueError(f'虚拟源拒绝非读取操作：{operation!r}')
        if not isinstance(arguments, dict):
            raise ValueError('源请求参数必须为对象')
        function = arguments.get('function')
        if type(function) is int and function in WRITE_FUNCTIONS:
            raise ValueError(f'虚拟源拒绝写功能码：{function}')
        if self.position >= len(self.steps):
            raise RuntimeError('源响应已经耗尽')
        step = self.steps[self.position]
        if step['operation'] != operation:
            raise ValueError('源操作顺序不符')
        if 'arguments' in step and step['arguments'] != arguments:
            raise ValueError('源请求参数不符')
        self.position += 1
        self.calls.append({'operation': operation, 'function': READ_OPERATIONS[operation],
                           'arguments': copy.deepcopy(arguments)})
        if 'error' in step:
            raise RuntimeError(step['error'])
        response = {'exception': step['exception']} if 'exception' in step else copy.deepcopy(step['response'])
        return response

    def remaining(self):
        return len(self.steps) - self.position
