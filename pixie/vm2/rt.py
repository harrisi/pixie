__config__ = None
py_list = list
py_str = str
from rpython.rlib.objectmodel import specialize, we_are_translated


def init():
    import pixie.vm2.code as code
    from pixie.vm2.object import affirm, _type_registry
    from rpython.rlib.rarithmetic import r_uint, base_int
    from rpython.rlib.rbigint import rbigint
    from pixie.vm2.primitives import nil, true, false
    from pixie.vm2.string import String
    from pixie.vm2.object import Object



    def unwrap(fn):
        if isinstance(fn, code.Var) and fn.is_defined() and hasattr(fn.deref(), "_returns"):
            tp = fn.deref()._returns
            if tp is bool:
                def wrapper(*args):
                    ret = fn.invoke(py_list(args))
                    if ret is nil or ret is false:
                        return False
                    return True
                return wrapper
            elif tp is r_uint:
                return lambda *args: fn.invoke(py_list(args)).r_uint_val()
            elif tp is unicode:
                def wrapper(*args):
                    ret = fn.invoke(py_list(args))
                    if ret is nil:
                        return None

                    if not isinstance(ret, String):
                        from pixie.vm2.object import runtime_error
                        runtime_error(u"Invalid return value, expected String")
                    return ret._str
                return wrapper
            else:
                assert False, "Don't know how to convert" + str(tp)
        return lambda *args: fn.invoke(py_list(args))

    if "__inited__" in globals():
        return

    import sys
    #sys.setrecursionlimit(10000)  # Yeah we blow the stack sometimes, we promise it's not a bug

    import pixie.vm2.code as code
    import pixie.vm2.numbers as numbers
    import pixie.vm2.files
    import pixie.vm2.stdlib
    import pixie.vm2.array
    import pixie.vm2.arraymap
    import pixie.vm2.custom_types
    import pixie.vm2.jit_tables
    import pixie.vm2.ffi
    import pixie.vm2.platform

    class FakeSpace(object):
        def wrap(self, x):
            if isinstance(x, Object):
                return x
            elif isinstance(x, bool):
                return true if x else false
            elif isinstance(x, int):
                return numbers.Integer(x)
            elif isinstance(x, rbigint):
                return numbers.BigInteger(x)
            elif isinstance(x, float):
                return numbers.Float(x)
            elif isinstance(x, unicode):
                return String(x)
            elif isinstance(x, py_str):
                return String(unicode(x))

            elif isinstance(x, r_uint):
                from pixie.vm2.numbers import SizeT
                return SizeT(x)


            elif x is None:
                return nil
            else:
                from pixie.vm2.object import runtime_error
                return runtime_error(u"Bad Wrap")
        wrap._annspecialcase_ = 'specialize:argtype(1)'

    space = FakeSpace()
    @specialize.argtype(0)
    def wrap(x):
        return space.wrap(x)

    globals()["wrap"] = wrap

    def int_val(x):
        affirm(isinstance(x, numbers.Number), u"Expected number")
        return x.int_val()

    globals()["int_val"] = int_val

    #f = open("pixie/stdlib.pxi")
    #data = f.read()
    #f.close()
    #rdr = reader.MetaDataReader(reader.StringReader(unicode(data)), u"pixie/stdlib.pixie")
    #result = nil
    #
    # @wrap_fn
    # def run_load_stdlib():
    #     with compiler.with_ns(u"pixie.stdlib"):
    #         while True:
    #             form = reader.read(rdr, False)
    #             if form is reader.eof:
    #                 return result
    #             result = compiler.compile(form).invoke([])
    #             reinit()
    #
    # stacklet.with_stacklets(run_load_stdlib)

    init_fns = [u"reduce", u"get", u"reset!", u"assoc", u"key", u"val", u"keys", u"vals", u"vec", u"load-file", u"compile-file",
                u"load-ns", u"hashmap", u"cons", u"-assoc", u"-val-at"]
    for x in init_fns:
        globals()[py_str(code.munge(x))] = unwrap(code.intern_var(u"pixie.stdlib", x))

    init_vars = [u"load-paths"]
    for x in init_vars:
        globals()[py_str(code.munge(x))] = code.intern_var(u"pixie.stdlib", x)

    #globals()[py_str(code.munge(u"ns"))] = NS_VAR

    globals()["__inited__"] = True

    globals()["is_true"] = lambda x: False if x is false or x is nil or x is None else True

    _type_registry.set_registry(code.ns_registry)

    numbers.init()
    code.init()

    import pixie.vm2.bits



def unwrap_string(x):
    from pixie.vm2.string import String
    from pixie.vm2.object import affirm
    from pixie.vm2.primitives import nil

    if x is None or x is nil:
        return None

    affirm(isinstance(x, String), u"Expected String")
    assert isinstance(x, String)

    return x._str

def unwrap_keyword(x):
    from pixie.vm2.keyword import Keyword
    from pixie.vm2.object import affirm

    affirm(isinstance(x, Keyword), u"Expected Keyword")
    assert isinstance(x, Keyword)

    return x._str




