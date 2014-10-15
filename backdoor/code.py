#!/usr/bin/env python

# Tools for working with code on the backdoored target machine.
# Assemble, disassemble, and compile. Requires an ARM cross-compiler.

CC      = 'arm-none-eabi-gcc'
OBJCOPY = 'arm-none-eabi-objcopy'
OBJDUMP = 'arm-none-eabi-objdump'

__all__ = ['disassemble_string', 'disassemble', 'assemble', 'compile', 'evalc']

import remote, os, random, struct
from subprocess import check_call, check_output
from dump import read_block


def tempnames(*suffixes):
    # Return temporary file names that are good enough for our purposes
    base = 'temp-coastermelt-' + str(random.randint(100000, 999999))
    return [base + s for s in suffixes]

def cleanup(filenames, leave_temp_files = False):
    if not leave_temp_files:
        for f in filenames:
            try:
                os.remove(f)
            except OSError:
                pass

def write_file(file, data):
    f = open(file, 'w')
    f.write(data)
    f.close()

def read_file(file):
    f = open(file, 'rb')
    d = f.read()
    f.close()
    return d


def disassemble_string(data, address = 0, thumb = True, leave_temp_files = False):
    """Disassemble code from a string buffer.
       Returns a string made of up multiple lines, each with
       the address in hex, a tab, then the disassembled code.
       """
    bin, = temps = tempnames('.bin')
    try:
        write_file(bin, data)

        text = check_output([
            OBJDUMP, '-D', '-w',
            '-b', 'binary', '-m', 'arm7tdmi', 
            '--prefix-addresses',
            '--adjust-vma', '0x%08x' % address,
            '-M', ('force-thumb', 'no-force-thumb')[not thumb],
            bin])

        lines = text.split('\n')
        lines = ['%s\t%s' % (l[2:10], l[11:]) for l in lines if l.startswith('0x')]
        return '\n'.join(lines)

    finally:
        cleanup(temps, leave_temp_files)

def disassemble(d, address, size, thumb = True, leave_temp_files = False):
    return disassemble_string(read_block(d, address, size), address, thumb, leave_temp_files)

def assemble(d, address, text, leave_temp_files = False):
    src, obj, bin, ld = temps = tempnames('.s', '.o', '.bin', '.ld')
    try:

        # Linker script
        write_file(ld, '''

            MEMORY {
                PATCH (rx) : ORIGIN = 0x%08x, LENGTH = 2048K
            }

            SECTIONS {
                .text : {
                    *(.text)
                } > PATCH
            }

            ''' % address)

        # Assembly source
        write_file(src, '''

            .text
            .syntax unified
            .thumb
            .global _start 
        _start:
            %s

            .align  2
            .pool
            .align  2

            ''' % text)

        check_call([ CC, '-nostdlib', '-nostdinc', '-o', obj, src, '-T', ld ])
        check_call([ OBJCOPY, obj, '-O', 'binary', bin ])
        data = read_file(bin)
        words = struct.unpack('<%dI' % (len(data)/4), data)

    finally:
        cleanup(temps, leave_temp_files)

    for i, word in enumerate(words):
        d.poke(address + 4*i, word)

def compile(d, address, expression, include = '',
    show_disassembly = False, thumb = True, leave_temp_files = False):

    src, obj, bin, ld = temps = tempnames('.cpp', '.o', '.bin', '.ld')
    try:

        # Uglier source here, a little nicer in 'show_disassembly'
        whole_src = ('  #include <stdint.h>\n'
                     '  %s\n'
                     '  uint32_t __attribute__ ((externally_visible, section(".first"))) start(unsigned arg) {\n'
                     '    return ( %s );\n'
                     '  }') % (include, expression)

        # Linker script
        write_file(ld, '''

            MEMORY {
                PATCH (rx) : ORIGIN = 0x%08x, LENGTH = 2048K
            }

            SECTIONS {
                .text : {
                    *(.first) *(.text) *(.rodata)
                } > PATCH
            }

            ''' % address)

        write_file(src, whole_src)
        check_call([ CC, '-nostdlib',
            '-o', obj, src, '-T', ld,
            '-Os', '-fwhole-program',
            ('-mthumb', '-mno-thumb')[not thumb]])
        
        check_call([ OBJCOPY, obj, '-O', 'binary', bin ])
        data = read_file(bin)

        if show_disassembly:
            print "========= C++ source ========= [%08x]" % address
            print whole_src
            print "========= Disassembly ========"
            print disassemble_string(data, address, thumb)
            print "=============================="

        words = struct.unpack('<%dI' % (len(data)/4), data)

    finally:
        cleanup(temps, leave_temp_files)

    for i, word in enumerate(words):
        d.poke(address + 4*i, word)


def evalc(d, expression, arg = 0, include = '', address = 0x1fffda0, show_disassembly = False):
    """Compile and remotely execute a C++ expression"""
    compile(d, address, expression, include, show_disassembly)
    return d.blx(address + 1, arg)[0]


if __name__ == '__main__':
    # Example

    d = remote.Device()
    pad = 0x1fffda0

    print disassemble(d, pad, 0x20)

    assemble(d, pad, 'nop\n' * 100)

    assemble(d, pad, '''
        nop
        bl      0x1fffd00
        ldr     r0, =0x1234abcd
        blx     r0
        bx      lr
        ''')

    print
    print disassemble(d, pad, 0x20)

    lib = '''
        int multiply(int a, int b) {
            return a * b;
        }
        '''

    # C++ one-liners
    compile(d, pad, 'multiply(arg, 5)', lib, show_disassembly = True)

    # Test the C++ function
    for n in 1, 2, 3, 500, 0:
        assert d.blx(pad+1, n)[0] == n * 5

    # An even higher level C++ example
    assert 5 == evalc(d, '5')
    assert 10 == evalc(d, 'arg', 10)
    assert 0xe59ff018 ==  evalc(d, '*(uint32_t*)0')

    print "\nSuccessfully called compiled C++ code on the target!"