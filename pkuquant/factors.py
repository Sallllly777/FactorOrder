"""Causal panels and an AST-interpreted, deliberately small expression language.

Price panels store values in each row's own raw-price units. Time-series operators
rebase historical observations with cumadj[s]/cumadj[t], so each row is point-in-time
forward-adjusted without a future end-of-sample anchor.
"""
import ast
import operator
import numpy as np
import pandas as pd


def _window(n):
    if isinstance(n, bool) or not isinstance(n, (int, np.integer)) or not 1 <= n <= 5000:
        raise ValueError('Window/delay must be an integer in [1, 5000]; future shifts are forbidden')
    return int(n)


class Panel:
    __array_priority__ = 1000

    def __init__(self, values, factor, universe, power=0):
        self.values = values.replace([np.inf, -np.inf], np.nan)
        self.factor, self.universe, self.power = factor, universe, power

    def new(self, values, power=None):
        return Panel(values, self.factor, self.universe, self.power if power is None else power)

    def continuous(self):
        return self.values if self.power == 0 else self.values * self.factor.pow(self.power)

    def rebase(self, values):
        return values if self.power == 0 else values / self.factor.pow(self.power)

    def _binary(self, other, fn, kind):
        if isinstance(other, Panel):
            if not self.values.index.equals(other.values.index) or not self.values.columns.equals(other.values.columns):
                raise ValueError('Panel alignment mismatch')
            if kind in ('add','compare') and self.power != other.power:
                raise ValueError('Incompatible price dimensions; normalize prices before combining with a score')
            p = self.power + other.power if kind == 'mul' else self.power - other.power if kind == 'div' else self.power
            rhs = other.values
        elif np.isscalar(other):
            rhs, p = other, self.power
        else:
            return NotImplemented
        if kind in ('compare','bool'):
            p = 0
        out = fn(self.values, rhs)
        if kind in ('compare','bool'):
            out = out.where(self.values.notna() & (other.values.notna() if isinstance(other, Panel) else True))
        return self.new(out, p)

    def __add__(self, x): return self._binary(x, operator.add, 'add')
    def __radd__(self, x): return self + x
    def __sub__(self, x): return self._binary(x, operator.sub, 'add')
    def __rsub__(self, x): return -self + x
    def __mul__(self, x): return self._binary(x, operator.mul, 'mul')
    def __rmul__(self, x): return self * x
    def __truediv__(self, x): return self._binary(x, operator.truediv, 'div')
    def __rtruediv__(self, x): return self.new(x / self.values, -self.power)
    def __neg__(self): return self.new(-self.values)
    def __pos__(self): return self
    def __abs__(self): return self.new(self.values.abs())
    def __gt__(self, x): return self._binary(x, operator.gt, 'compare')
    def __ge__(self, x): return self._binary(x, operator.ge, 'compare')
    def __lt__(self, x): return self._binary(x, operator.lt, 'compare')
    def __le__(self, x): return self._binary(x, operator.le, 'compare')
    def __eq__(self, x): return self._binary(x, operator.eq, 'compare')
    def __ne__(self, x): return self._binary(x, operator.ne, 'compare')
    def __and__(self, x): return self._binary(x, lambda a,b: a.fillna(False).astype(bool) & (b.fillna(False).astype(bool) if isinstance(b,pd.DataFrame) else bool(b)), 'bool')
    def __or__(self, x): return self._binary(x, lambda a,b: a.fillna(False).astype(bool) | (b.fillna(False).astype(bool) if isinstance(b,pd.DataFrame) else bool(b)), 'bool')
    def __invert__(self): return self.new(~self.values.fillna(False).astype(bool), 0)
    def __bool__(self): raise TypeError('Use &, | and parentheses, not Python and/or')
    def __pow__(self, n):
        if not isinstance(n,(int,float)) or not np.isfinite(n) or abs(n) > 8:
            raise ValueError('Power must be a finite scalar between -8 and 8')
        return self.new(self.values.pow(n), self.power*n)

    def to_pandas(self):
        return self.values.copy()


def rank(x):
    """Cross-sectional ascending percentile rank, average ties, today's universe only."""
    return x.new(x.values.where(x.universe).rank(axis=1, pct=True), 0)


def zscore(x):
    v = x.values.where(x.universe)
    return x.new(v.sub(v.mean(axis=1), axis=0).div(v.std(axis=1).replace(0,np.nan), axis=0), 0)


def scale(x):
    v = x.values.where(x.universe)
    return x.new(v.div(v.abs().sum(axis=1).replace(0,np.nan), axis=0), 0)


def ts_delay(x, d=1):
    return x.new(x.rebase(x.continuous().shift(_window(d))))


def ts_delta(x, d=1): return x - ts_delay(x,d)
def ts_return(x, d=1):
    # Divide the continuous series directly; avoid an extra rebase/divide pair
    # that can split mathematically identical returns into different rank ties.
    base = x.continuous()
    return x.new(base / base.shift(_window(d)) - 1, 0)


def _rolling(x, d, name):
    d = _window(d)
    return x.new(x.rebase(getattr(x.continuous().rolling(d, min_periods=d), name)()))


def ts_mean(x, d): return _rolling(x,d,'mean')
def ts_sum(x, d): return _rolling(x,d,'sum')
def ts_std(x, d): return _rolling(x,d,'std')
def ts_min(x, d): return _rolling(x,d,'min')
def ts_max(x, d): return _rolling(x,d,'max')


def ts_rank(x,d):
    d = _window(d)
    return x.new(x.continuous().rolling(d,min_periods=d).rank(pct=True),0)


def ts_corr(x,y,d):
    d = _window(d)
    return x.new(x.continuous().rolling(d,min_periods=d).corr(y.continuous()),0)


def decay_linear(x,d):
    # Full-window weighted mean; no warmup filling or future values.
    d = _window(d)
    base = x.continuous()
    if d == 1: return x
    indices = pd.Series(np.arange(len(base),dtype=float),index=base.index)
    total = base.mul(indices,axis=0).rolling(d,min_periods=d).sum()
    total -= base.rolling(d,min_periods=d).sum().mul(indices-d,axis=0)
    return x.new(x.rebase(total / (d*(d+1)/2)))


def where(condition, yes, no):
    template = yes if isinstance(yes,Panel) else no if isinstance(no,Panel) else condition
    p = template.power if isinstance(yes,Panel) or isinstance(no,Panel) else 0
    if isinstance(yes,Panel) and isinstance(no,Panel) and yes.power != no.power:
        raise ValueError('where branches must have matching price dimensions')
    y = yes.values if isinstance(yes,Panel) else yes
    n = no.values if isinstance(no,Panel) else no
    return template.new(pd.DataFrame(np.where(condition.values.fillna(False).astype(bool),y,n),index=template.values.index,columns=template.values.columns),p)


def clip(x, lower, upper):
    if lower > upper: raise ValueError('lower > upper')
    return x.new(x.values.clip(lower,upper))


def log(x):
    if x.power != 0:
        raise ValueError('log expects a dimensionless ratio, e.g. log(close/ts_delay(close,1))')
    return x.new(np.log(x.values.where(x.values > 0)),0)


def sign(x): return x.new(np.sign(x.values),0)


def trade_when(trigger, alpha, exit):
    """Exit > 0 clears score; trigger > 0 updates; otherwise holds prior score.

    This retains a factor value, NOT a brokerage order or position.
    """
    a = alpha.continuous().to_numpy()
    t = trigger.values.to_numpy() > 0
    e = exit.values.to_numpy() > 0 if isinstance(exit,Panel) else np.full_like(t,exit > 0)
    out = np.full_like(a,np.nan,dtype=float)
    prev = np.full(a.shape[1],np.nan)
    for i in range(len(a)):
        prev = np.where(e[i],np.nan,np.where(t[i],a[i],prev))
        out[i] = prev
    return alpha.new(alpha.rebase(pd.DataFrame(out,index=alpha.values.index,columns=alpha.values.columns)))


OPERATORS = {fn.__name__: fn for fn in (rank,zscore,scale,ts_delay,ts_delta,ts_return,ts_mean,ts_sum,ts_std,ts_min,ts_max,ts_rank,ts_corr,decay_linear,where,clip,log,sign,trade_when)}
OPERATORS.update({'abs': abs, 'delay': ts_delay, 'delta': ts_delta, 'ts_std_dev': ts_std})


class Fields:
    def __init__(self, data, universe):
        self._data, self._universe = data, universe

    def __getattr__(self,name):
        if name == 'volume' and name not in self._data.frames:
            raise ValueError('volume is unavailable: num_trades is not share volume')
        if name == 'returns':
            return ts_return(self.close,1)
        if name not in ('open','high','low','close','amt','num_trades','volume'):
            raise ValueError(f'Unknown field: {name}')
        return Panel(self._data.frames[name],self._data.frames['cumadj'],self._universe,1 if name in ('open','high','low','close') else 0)


class Expression:
    """No eval, exec, attribute access, imports, indexing, loops or arbitrary calls."""
    def __init__(self,text):
        if not isinstance(text,str) or len(text) > 6000:
            raise ValueError('Expression must be a string of at most 6000 characters')
        self.text = text
        self.tree = ast.parse(text,mode='exec')
        if len(list(ast.walk(self.tree))) > 512:
            raise ValueError('Expression too complex')

    def evaluate(self,fields):
        local = {}
        fields_used = set()

        def visit(n):
            if isinstance(n,ast.Constant) and type(n.value) in (int,float,bool):
                if abs(n.value)>1e12 or not np.isfinite(n.value): raise ValueError('Invalid/oversized constant')
                return n.value
            if isinstance(n,ast.Name):
                if n.id in local: return local[n.id]
                if n.id not in ('open','high','low','close','amt','num_trades','returns','volume'):
                    raise ValueError(f'Unknown or private field: {n.id}')
                fields_used.add(n.id)
                return getattr(fields,n.id)
            if isinstance(n,ast.BinOp):
                fn = {ast.Add:operator.add,ast.Sub:operator.sub,ast.Mult:operator.mul,ast.Div:operator.truediv,ast.Pow:operator.pow,ast.BitAnd:operator.and_,ast.BitOr:operator.or_}.get(type(n.op))
                if fn:
                    left,right = visit(n.left),visit(n.right)
                    if isinstance(n.op,ast.Pow) and (not isinstance(right,(int,float)) or abs(right)>8):
                        raise ValueError('Exponent must be a scalar between -8 and 8')
                    return fn(left,right)
            if isinstance(n,ast.UnaryOp):
                fn = {ast.USub:operator.neg,ast.UAdd:operator.pos,ast.Invert:operator.invert}.get(type(n.op))
                if fn: return fn(visit(n.operand))
            if isinstance(n,ast.Compare) and len(n.ops)==1:
                fn = {ast.Gt:operator.gt,ast.GtE:operator.ge,ast.Lt:operator.lt,ast.LtE:operator.le,ast.Eq:operator.eq,ast.NotEq:operator.ne}.get(type(n.ops[0]))
                if fn: return fn(visit(n.left),visit(n.comparators[0]))
            if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in OPERATORS and not n.keywords:
                return OPERATORS[n.func.id](*(visit(a) for a in n.args))
            raise ValueError(f'Forbidden syntax: {ast.dump(n,include_attributes=False)[:120]}')

        value = None
        for i,node in enumerate(self.tree.body):
            if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name):
                name = node.targets[0].id
                if name.startswith('_') or name in OPERATORS or name in ('open','high','low','close','amt','num_trades','returns','volume','cumadj'):
                    raise ValueError(f'Reserved variable: {name}')
                local[name] = visit(node.value)
            elif isinstance(node,ast.Expr) and i == len(self.tree.body)-1:
                value = visit(node.value)
            else:
                raise ValueError('Use optional assignments followed by one final factor expression')
        if not isinstance(value,Panel): raise ValueError('Expression must produce a panel, not a scalar')
        return value, fields_used
