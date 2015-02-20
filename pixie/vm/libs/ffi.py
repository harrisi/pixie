py_object = object
import rpython.rlib.rdynload as dynload
import pixie.vm.object as object
from pixie.vm.object import runtime_error
from pixie.vm.keyword import Keyword
import pixie.vm.stdlib  as proto
from pixie.vm.code import as_var, affirm, extend
import pixie.vm.rt as rt
from rpython.rtyper.lltypesystem import rffi, lltype, llmemory
from pixie.vm.primitives import nil, true, false
from pixie.vm.numbers import Integer, Float
from pixie.vm.string import String
from pixie.vm.keyword import Keyword
from pixie.vm.util import unicode_to_utf8
from rpython.rlib import clibffi
from rpython.rlib.jit_libffi import jit_ffi_call, CIF_DESCRIPTION, CIF_DESCRIPTION_P, \
    FFI_TYPE_P, FFI_TYPE_PP, SIZE_OF_FFI_ARG
import rpython.rlib.jit_libffi as jit_libffi
from rpython.rlib.objectmodel import keepalive_until_here, we_are_translated
import rpython.rlib.jit as jit
from rpython.rlib.rarithmetic import intmask


"""
FFI interface for pixie.

This code gets a bit interesting. We use the RPython rlib module jit_libffi to do the interfacing, you can find
good docs in that module.

"""


class CType(object.Type):
    def __init__(self, name):
        object.Type.__init__(self, name)


class ExternalLib(object.Object):
    _type = object.Type(u"pixie.stdlib.ExternalLib")

    def type(self):
        return ExternalLib._type

    def __init__(self, nm):
        assert isinstance(nm, unicode)
        self._name = nm
        self._is_inited = False
        self.load_lib()

    def load_lib(self):
        if not self._is_inited:
            load_paths = rt.deref(rt.deref(rt.load_paths))

            for x in range(rt.count(load_paths)):
                s = rffi.str2charp(str(rt.name(rt.nth(load_paths, rt.wrap(x)))) + "/" + str(self._name))
                try:
                    self._dyn_lib = dynload.dlopen(s)
                    self._is_inited = True
                except dynload.DLOpenError as ex:
                    continue
                finally:
                    rffi.free_charp(s)
                break

            if not self._is_inited:
                s = rffi.str2charp(str(self._name))
                try:
                    self._dyn_lib = dynload.dlopen(s)
                    self._is_inited = True
                except dynload.DLOpenError as ex:
                    raise object.WrappedException(object.RuntimeException(rt.wrap(u"Couldn't Load Library: " + self._name)))
                finally:
                    rffi.free_charp(s)








    def get_fn_ptr(self, nm):
        assert isinstance(nm, unicode)
        s = rffi.str2charp(str(nm))
        sym = dynload.dlsym(self._dyn_lib, s)
        rffi.free_charp(s)
        return sym

class FFIFn(object.Object):
    _type = object.Type(u"pixie.stdlib.FFIFn")
    _immutable_fields_ = ["_is_inited", "_lib", "_name", "_arg_types[*]", "_arity", "_ret_type", "_is_variadic", \
                            "_transfer_size", "_arg0_offset", "_ret_offset", "_cd"]

    def type(self):
        return FFIFn._type

    def __init__(self, lib, name, arg_types, ret_type, is_variadic):
        self._rev = 0
        self._name = name
        self._lib = lib
        self._arg_types = arg_types
        self._arity = len(arg_types)
        self._ret_type = ret_type
        self._is_variadic = is_variadic
        self._f_ptr = self._lib.get_fn_ptr(self._name)
        self._cd = CifDescrBuilder(self._arg_types, self._ret_type).rawallocate()

    @jit.unroll_safe
    def prep_exb(self, args):
        size = jit.promote(self._cd.exchange_size)
        exb = rffi.cast(rffi.VOIDP, lltype.malloc(rffi.CCHARP.TO, size, flavor="raw"))
        tokens = [None] * len(args)

        for i, tp in enumerate(self._arg_types):
            offset_p = rffi.ptradd(exb, jit.promote(self._cd.exchange_args[i]))
            tokens[i] = tp.ffi_set_value(offset_p, args[i])

        return exb, tokens

    def get_ret_val_from_buffer(self, exb):
        offset_p = rffi.ptradd(exb, jit.promote(self._cd.exchange_result_libffi))
        ret_val = self._ret_type.ffi_get_value(offset_p)
        return ret_val

    @jit.unroll_safe
    def _invoke(self, args):
        arity = len(args)
        if self._is_variadic:
            if arity < self._arity:
                runtime_error(u"Wrong number of args to fn: got " + unicode(str(arity)) +
                    u", expected at least " + unicode(str(self._arity)))
        else:
            if arity != self._arity:
                runtime_error(u"Wrong number of args to fn: got " + unicode(str(arity)) +
                    u", expected " + unicode(str(self._arity)))

        exb, tokens = self.prep_exb(args)
        cd = jit.promote(self._cd)
        #fp = jit.promote(self._f_ptr)
        jit_ffi_call(cd,
                     self._f_ptr,
                     exb)
        ret_val = self.get_ret_val_from_buffer(exb)

        for x in range(len(args)):
            t = tokens[x]
            if t is not None:
                t.finalize_token()

        lltype.free(exb, flavor="raw")
        keepalive_until_here(args)
        return ret_val

    def invoke(self, args):
        self = jit.promote(self)
        return self._invoke(args)

@as_var("ffi-library")
def _ffi_library(ns):
    nm = rt.name(ns)
    return ExternalLib(nm)

@as_var("ffi-fn")
def _ffi_fn__args(args):
    affirm(len(args) >= 4, u"ffi-fn requires at least 4 arguments")
    lib, nm, arg_types, ret_type = args[:4]

    affirm(isinstance(lib, ExternalLib), u"First argument must be an ExternalLib")
    affirm(isinstance(ret_type, object.Type), u"Ret type must be a type")
    affirm(rt.namespace(nm) is None, u"Name must not be namespaced")

    cnt = rt.count(arg_types)
    new_args = [None] * cnt
    for x in range(cnt):
        t = rt.nth(arg_types, rt.wrap(x))
        affirm(isinstance(t, object.Type), u"Arg defs must be types")
        new_args[x] = t

    kwargs = args[4:]
    affirm(len(kwargs) & 0x1 == 0, u"ffi-fn requires even number of options")

    is_variadic = False
    for i in range(0, len(kwargs)/2, 2):
        key = kwargs[i]
        val = kwargs[i+1]
        
        affirm(isinstance(key, Keyword), u"ffi-fn options should be keyword/bool pairs")
        affirm(val is true or val is false, u"ffi-fn options should be keyword/bool pairs")

        k = rt.name(key)
        if k == u"variadic?":
            is_variadic = True if val is true else False
        else:
            affirm(False, u"unknown ffi-fn option: :" + k)

    f = FFIFn(lib, rt.name(nm), new_args, ret_type, is_variadic)
    return f






class Buffer(object.Object):
    """ Defines a byte buffer with non-gc'd (therefore non-movable) contents
    """
    _type = object.Type(u"pixie.stdlib.Buffer")

    def type(self):
        return Buffer._type

    def __init__(self, size):
        self._size = size
        self._used_size = 0
        self._buffer = lltype.malloc(rffi.CCHARP.TO, size, flavor="raw")


    def __del__(self):
        #lltype.free(self._buffer, flavor="raw")
        pass

    def set_used_size(self, size):
        self._used_size = size

    def buffer(self):
        return self._buffer

    def raw_data(self):
        return rffi.cast(rffi.VOIDP, self._buffer)

    def count(self):
        return self._used_size

    def nth_char(self, idx):
        assert isinstance(idx, int)
        return self._buffer[idx]

    def capacity(self):
        return self._size


@extend(proto._nth, Buffer)
def _nth(self, idx):
    return rt.wrap(ord(self.nth_char(idx.int_val())))

@extend(proto._count, Buffer)
def _count(self):
    return rt.wrap(intmask(self.count()))

@as_var("buffer")
def buffer(size):
    return Buffer(size.int_val())

@as_var("buffer-capacity")
def buffer_capacity(buffer):
    return rt.wrap(intmask(buffer.capacity()))

@as_var("set-buffer-count!")
def set_buffer_size(self, size):
    self.set_used_size(size.int_val())
    return self

def make_itype(name, ctype, llt):
    lltp = lltype.Ptr(lltype.Array(llt, hints={'nolength': True}))
    class GenericCInt(CType):
        def __init__(self):
            CType.__init__(self, name)

        def ffi_get_value(self, ptr):
            casted = rffi.cast(lltp, ptr)
            return Integer(rffi.cast(rffi.LONG, casted[0]))

        def ffi_set_value(self, ptr, val):
            casted = rffi.cast(lltp, ptr)
            casted[0] = rffi.cast(llt, val.int_val())

        def ffi_size(self):
            return rffi.sizeof(llt)

        def ffi_type(self):
            return ctype

    return GenericCInt()

from rpython.rlib.rarithmetic import build_int
for x in [8, 16, 32, 64]:
    for s in [True, False]:
        nm = "C" + ("" if s else "U") + "Int" + str(x)
        int_tp = lltype.build_number(None, build_int(nm, s, x))
        ctype = clibffi.cast_type_to_ffitype(int_tp)
        make_itype(unicode("pixie.stdlib." + nm), ctype, int_tp)







class Token(py_object):
    """ Tokens are returned by ffi_set_value and are called when ffi is ready to clean up resources
    """
    def finalize_token(self):
        pass



class CInt(CType):
    def __init__(self):
        CType.__init__(self, u"pixie.stdlib.CInt")

    def ffi_get_value(self, ptr):
        casted = rffi.cast(rffi.INTP, ptr)
        return Integer(rffi.cast(rffi.LONG, casted[0]))

    def ffi_set_value(self, ptr, val):
        casted = rffi.cast(rffi.INTP, ptr)
        casted[0] = rffi.cast(rffi.INT, val.int_val())

    def ffi_size(self):
        return rffi.sizeof(rffi.INT)

    def ffi_type(self):
        return clibffi.cast_type_to_ffitype(rffi.INT)
CInt()

class CDouble(CType):
    def __init__(self):
        CType.__init__(self, u"pixie.stdlib.CDouble")

    def ffi_get_value(self, ptr):
        casted = rffi.cast(rffi.DOUBLEP, ptr)
        return Float(casted[0])

    def ffi_set_value(self, ptr, val):
        casted = rffi.cast(rffi.DOUBLEP, ptr)
        casted[0] = rffi.cast(rffi.DOUBLE, val.float_val())

    def ffi_size(self):
        return rffi.sizeof(rffi.DOUBLE)

    def ffi_type(self):
        return clibffi.cast_type_to_ffitype(rffi.DOUBLE)
CDouble()

class CCharP(CType):
    def __init__(self):
        CType.__init__(self, u"pixie.stdlib.CCharP")

    def ffi_get_value(self, ptr):
        casted = rffi.cast(rffi.CCHARPP, ptr)
        if casted[0] == lltype.nullptr(rffi.CCHARP.TO):
            return nil
        else:
            return String(unicode(rffi.charp2str(casted[0])))

    def ffi_set_value(self, ptr, val):
        pnt = rffi.cast(rffi.CCHARPP, ptr)
        utf8 = unicode_to_utf8(rt.name(val))
        raw = rffi.str2charp(utf8)
        pnt[0] = raw
        return CCharPToken(raw)

    def ffi_size(self):
        return rffi.sizeof(rffi.CCHARP)

    def ffi_type(self):
        return clibffi.ffi_type_pointer
CCharP()

class CCharPToken(Token):
    def __init__(self, raw):
        self._raw = raw

    def finalize_token(self):
        rffi.free_charp(self._raw)



class CVoid(CType):
    def __init__(self):
        CType.__init__(self, u"pixie.stdlib.CVoid")

    def ffi_get_value(self, ptr):
        return nil

    def ffi_set_value(self, ptr, val):
        runtime_error(u"Can't encode a Void")

    def ffi_size(self):
        return rffi.sizeof(rffi.VOIDP)

    def ffi_type(self):
        return clibffi.ffi_type_pointer

cvoid = CVoid()

class CVoidP(CType):
    def __init__(self):
        CType.__init__(self, u"pixie.stdlib.CVoidP")

    def ffi_get_value(self, ptr):
        casted = rffi.cast(rffi.VOIDPP, ptr)
        if casted[0] == lltype.nullptr(rffi.VOIDP.TO):
            return nil
        else:
            return VoidP(casted[0])

    def ffi_set_value(self, ptr, val):
        pnt = rffi.cast(rffi.VOIDPP, ptr)
        if isinstance(val, Buffer):
            pnt[0] = val.buffer()
        elif isinstance(val, VoidP):
            pnt[0] = val.raw_data()
        elif val is nil:
            pnt[0] = rffi.cast(rffi.VOIDP, 0)
        elif isinstance(val, CStruct):
            pnt[0] = rffi.cast(rffi.VOIDP, val.raw_data())
        else:
            print val
            affirm(False, u"Cannot encode this type")

    def ffi_size(self):
        return rffi.sizeof(rffi.VOIDP)

    def ffi_type(self):
        return clibffi.ffi_type_pointer
cvoidp = CVoidP()

class VoidP(object.Object):
    def type(self):
        return cvoidp

    def __init__(self, raw_data):
        self._raw_data = raw_data

    def raw_data(self):
        return rffi.cast(rffi.VOIDP, self._raw_data)

@as_var(u"pixie.ffi", u"unpack")
def unpack(ptr, offset, tp):
    affirm(isinstance(ptr, VoidP) or isinstance(ptr, Buffer) or isinstance(ptr, CStruct), u"Type is not unpackable")
    affirm(isinstance(tp, CType), u"Packing type must be a CType")
    ptr = rffi.ptradd(ptr.raw_data(), offset.int_val())
    return tp.ffi_get_value(ptr)

@as_var(u"pixie.ffi", u"pack!")
def pack(ptr, offset, tp, val):
    affirm(isinstance(ptr, VoidP) or isinstance(ptr, Buffer) or isinstance(ptr, CStruct), u"Type is not unpackable")
    affirm(isinstance(tp, CType), u"Packing type must be a CType")
    ptr = rffi.ptradd(ptr.raw_data(), offset.int_val())
    tp.ffi_set_value(ptr, val)
    return nil


class CStructType(object.Type):
    base_type = object.Type(u"pixie.ffi.CStruct")
    _immutable_fields_ = ["_desc", "_size"]

    def __init__(self, name, size, desc):
        object.Type.__init__(self, name, CStructType.base_type)
        self._desc = desc
        self._size = size
        #offsets is a dict of {nm, (type, offset)}

    def get_offset(self, nm):
        (tp, offset) = self._desc.get(nm, (None, 0))

        assert tp is not None

        return offset

    def get_type(self, nm):
        (tp, offset) = self._desc.get(nm, (None, 0))

        assert tp is not None

        return tp

    def cast_to(self, frm):
        return CStruct(self, frm.raw_data())

    @jit.elidable_promote()
    def get_desc(self, nm):
        return self._desc.get(nm, (None, 0))

    def invoke(self, args):
        return CStruct(self, rffi.cast(rffi.VOIDP, lltype.malloc(rffi.CCHARP.TO, self._size, flavor="raw")))


registered_callbacks = {}

class CCallback(object.Object):
    _type = object.Type(u"pixie.ffi.CCallback")

    def __init__(self, cft, raw_closure, id, fn):
        self._fn = fn
        self._cft = cft
        self._raw_closure = raw_closure
        self._is_invoked = False
        self._unique_id = id

    def type(self):
        return CCallback._type

    def get_raw_closure(self):
        return self._raw_closure

    def ll_invoke(self, llargs, llres):
        cft = self._cft
        assert isinstance(cft, CFunctionType)

        args = [None] * len(cft._arg_types)
        for i, tp in enumerate(cft._arg_types):
            args[i] = tp.ffi_get_value(llargs[i])

        self._is_invoked = True
        retval = self._fn.invoke(args)
        if cft._ret_type is not cvoid:
            cft._ret_type.ffi_set_value(llres, retval)


    def cleanup(self):
        del registered_callbacks[self._unique_id]
        clibffi.closureHeap.free(self._raw_closure)

@extend(proto._dispose_BANG_, CCallback)
def _dispose(self):
    self.cleanup()



@as_var(u"ffi-callback")
def ffi_callback(args, ret_type):
    args_w = [None] * rt.count(args)

    for x in range(rt.count(args)):
        arg = rt.nth(args, rt.wrap(x))
        if not isinstance(arg, object.Type):
            runtime_error(u"Expected type, got " + rt.name(rt.str(arg)))
        args_w[x] = arg

    if not isinstance(ret_type, object.Type):
        runtime_error(u"Expected type, got " + rt.name(rt.str(ret_type)))

    return CFunctionType(args_w, ret_type)

@as_var(u"ffi-prep-callback")
def ffi_prep_callback(tp, f):
    affirm(isinstance(tp, CFunctionType), u"First argument to ffi-prep-callback must be a CFunctionType")
    raw_closure = rffi.cast(rffi.VOIDP, clibffi.closureHeap.alloc())

    unique_id = len(registered_callbacks)

    res = clibffi.c_ffi_prep_closure(rffi.cast(clibffi.FFI_CLOSUREP, raw_closure), tp.get_cd().cif,
                             invoke_callback,
                             rffi.cast(rffi.VOIDP, unique_id))


    if rffi.cast(lltype.Signed, res) != clibffi.FFI_OK:
        registered_callbacks[unique_id] = None
        runtime_error(u"libffi failed to build this callback")

    cb = CCallback(tp, raw_closure, unique_id, f)
    registered_callbacks[unique_id] = cb

    return cb

# Callback Code

@jit.jit_callback("CFFI")
def invoke_callback(ffi_cif, ll_res, ll_args, ll_userdata):
    cb = registered_callbacks[rffi.cast(rffi.INT_real, ll_userdata)]
    cb.ll_invoke(ll_args, ll_res)

class FunctionTypeNameGenerator(py_object):
    def __init__(self):
        self._idx = 0
    def next(self):
        self._idx += 1
        return unicode("CFunctionType" + str(self._idx))

name_gen = FunctionTypeNameGenerator()

class CFunctionType(object.Type):
    base_type = object.Type(u"pixie.ffi.CType")
    _immutable_fields_ = ["_arg_types", "_ret-type", "_cd"]

    def __init__(self, arg_types, ret_type):
        object.Type.__init__(self, name_gen.next(), CStructType.base_type)
        self._arg_types = arg_types
        self._ret_type = ret_type
        self._cd = CifDescrBuilder(self._arg_types, self._ret_type).rawallocate()

    def ffi_get_value(self, ptr):
        runtime_error(u"Cannot get a callback value via FFI")

    def ffi_set_value(self, ptr, val):
        affirm(isinstance(val, CCallback), u"Can only encode CCallbacks as function pointers")


        casted = rffi.cast(rffi.VOIDPP, ptr)
        casted[0] = val.get_raw_closure()

        return None

    def get_cd(self):
        return self._cd

    def ffi_size(self):
        return rffi.sizeof(rffi.VOIDP)

    def ffi_type(self):
        return clibffi.ffi_type_pointer

class CStruct(object.Object):
    _immutable_fields_ = ["_type", "_buffer"]
    def __init__(self, tp, buffer):
        self._type = tp
        self._buffer = buffer

    def type(self):
        return self._type

    def raw_data(self):
        return rffi.cast(rffi.VOIDP, self._buffer)

    def val_at(self, k, not_found):
        (tp, offset) = self._type.get_desc(k)

        if tp is None:
            return not_found

        offset = rffi.ptradd(self._buffer, offset)
        return tp.ffi_get_value(rffi.cast(rffi.VOIDP, offset))

    def set_val(self, k, v):
        (tp, offset) = self._type.get_desc(k)

        if tp is None:
            runtime_error(u"Invalid field name: " + rt.name(rt.str(tp)))

        offset = rffi.ptradd(self._buffer, offset)
        tp.ffi_set_value(rffi.cast(rffi.VOIDP, offset), v)

        return nil


@as_var("pixie.ffi", "c-struct")
def c_struct(name, size, spec):
    d = {}
    for x in range(rt.count(spec)):
        row = rt.nth(spec, rt.wrap(x))
        nm = rt.nth(row, rt.wrap(0))
        tp = rt.nth(row, rt.wrap(1))
        offset = rt.nth(row, rt.wrap(2))

        affirm(isinstance(nm, Keyword), u"c-struct field names must be keywords")
        if not isinstance(tp, CType):
            runtime_error(u"c-struct field types must be c types, got: " + rt.name(rt.str(tp)))

        d[nm] = (tp, offset.int_val())

    return CStructType(rt.name(name), size.int_val(), d)

@as_var("pixie.ffi", "cast")
def c_cast(frm, to):
    if not isinstance(to, CStructType):
        runtime_error(u"Expected a CStruct type to cast to, got " + rt.name(rt.str(to)))

    if not isinstance(frm, CStruct) and not isinstance(frm, VoidP):
        runtime_error(u"From must be a CVoidP or a CStruct, got " + rt.name(rt.str(frm)))

    return to.cast_to(frm)

@as_var("pixie.ffi", "free")
def c_free(frm):
    if not isinstance(frm, CStruct) and not isinstance(frm, VoidP):
        runtime_error(u"Can only free CStructs or CVoidP")

    lltype.free(frm.raw_data(), flavor="raw")

    return nil

@extend(proto._val_at, CStructType.base_type)
def val_at(self, k, not_found):
    return self.val_at(k, not_found)

@as_var("pixie.ffi", "set!")
def set_(self, k, val):
    return self.set_val(k, val)




import sys

BIG_ENDIAN = sys.byteorder == 'big'
USE_C_LIBFFI_MSVC = getattr(clibffi, 'USE_C_LIBFFI_MSVC', False)



class CifDescrBuilder(py_object):
    rawmem = lltype.nullptr(rffi.CCHARP.TO)

    def __init__(self, fargs, fresult):
        self.fargs = fargs
        self.fresult = fresult

    def fb_alloc(self, size):
        size = llmemory.raw_malloc_usage(size)
        if not self.bufferp:
            self.nb_bytes += size
            return lltype.nullptr(rffi.CCHARP.TO)
        else:
            result = self.bufferp
            self.bufferp = rffi.ptradd(result, size)
            return result

    def fb_fill_type(self, ctype, is_result_type):
        return ctype.ffi_type()


    def fb_build(self):
        # Build a CIF_DESCRIPTION.  Actually this computes the size and
        # allocates a larger amount of data.  It starts with a
        # CIF_DESCRIPTION and continues with data needed for the CIF:
        #
        #  - the argument types, as an array of 'ffi_type *'.
        #
        #  - optionally, the result's and the arguments' ffi type data
        #    (this is used only for 'struct' ffi types; in other cases the
        #    'ffi_type *' just points to static data like 'ffi_type_sint32').
        #
        nargs = len(self.fargs)

        # start with a cif_description (cif and exchange_* fields)
        self.fb_alloc(llmemory.sizeof(CIF_DESCRIPTION, nargs))

        # next comes an array of 'ffi_type*', one per argument
        atypes = self.fb_alloc(rffi.sizeof(FFI_TYPE_P) * nargs)
        self.atypes = rffi.cast(FFI_TYPE_PP, atypes)

        # next comes the result type data
        self.rtype = self.fb_fill_type(self.fresult, True)

        # next comes each argument's type data
        for i, farg in enumerate(self.fargs):
            atype = self.fb_fill_type(farg, False)
            if self.atypes:
                self.atypes[i] = atype

    def align_arg(self, n):
        return (n + 7) & ~7

    def fb_build_exchange(self, cif_descr):
        nargs = len(self.fargs)

        # first, enough room for an array of 'nargs' pointers
        exchange_offset = rffi.sizeof(rffi.VOIDP) * nargs
        exchange_offset = self.align_arg(exchange_offset)
        cif_descr.exchange_result = exchange_offset
        cif_descr.exchange_result_libffi = exchange_offset

        if BIG_ENDIAN and self.fresult.is_primitive_integer:
            # For results of precisely these types, libffi has a
            # strange rule that they will be returned as a whole
            # 'ffi_arg' if they are smaller.  The difference
            # only matters on big-endian.
            if self.fresult.size < SIZE_OF_FFI_ARG:
                diff = SIZE_OF_FFI_ARG - self.fresult.size
                cif_descr.exchange_result += diff

        # then enough room for the result, rounded up to sizeof(ffi_arg)
        exchange_offset += max(rffi.getintfield(self.rtype, 'c_size'),
                               SIZE_OF_FFI_ARG)

        # loop over args
        for i, farg in enumerate(self.fargs):
            #if isinstance(farg, W_CTypePointer):
            #    exchange_offset += 1   # for the "must free" flag
            exchange_offset = self.align_arg(exchange_offset)
            cif_descr.exchange_args[i] = exchange_offset
            exchange_offset += rffi.getintfield(self.atypes[i], 'c_size')

        # store the exchange data size
        cif_descr.exchange_size = exchange_offset

    def fb_extra_fields(self, cif_descr):
        cif_descr.abi = clibffi.FFI_DEFAULT_ABI    # XXX
        cif_descr.nargs = len(self.fargs)
        cif_descr.rtype = self.rtype
        cif_descr.atypes = self.atypes

    @jit.dont_look_inside
    def rawallocate(self):

        # compute the total size needed in the CIF_DESCRIPTION buffer
        self.nb_bytes = 0
        self.bufferp = lltype.nullptr(rffi.CCHARP.TO)
        self.fb_build()

        # allocate the buffer
        if we_are_translated():
            rawmem = lltype.malloc(rffi.CCHARP.TO, self.nb_bytes,
                                   flavor='raw')
            rawmem = rffi.cast(CIF_DESCRIPTION_P, rawmem)
        else:
            # gross overestimation of the length below, but too bad
            rawmem = lltype.malloc(CIF_DESCRIPTION_P.TO, self.nb_bytes,
                                   flavor='raw')

        # the buffer is automatically managed from the W_CTypeFunc instance
        # ctypefunc._cd = rawmem

        # call again fb_build() to really build the libffi data structures
        self.bufferp = rffi.cast(rffi.CCHARP, rawmem)
        self.fb_build()
        assert self.bufferp == rffi.ptradd(rffi.cast(rffi.CCHARP, rawmem),
                                           self.nb_bytes)

        # fill in the 'exchange_*' fields
        self.fb_build_exchange(rawmem)

        # fill in the extra fields
        self.fb_extra_fields(rawmem)

        # call libffi's ffi_prep_cif() function
        res = jit_libffi.jit_ffi_prep_cif(rawmem)
        if res != clibffi.FFI_OK:
            runtime_error(u"libffi failed to build function type")

        return rawmem

    def get_item(self, nm):
        (tp, offset) = self._type.get_desc(nm)
        ptr = rffi.ptradd(self._buffer, offset)
        return tp.ffi_load_from(ptr)


# Taken from PyPy

