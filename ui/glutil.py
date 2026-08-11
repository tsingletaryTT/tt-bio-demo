"""Shader compilation helpers. Requires a current GL context."""

from OpenGL import GL


class GLError(Exception):
    """A shader failed to compile or link."""


def _compile_shader(source, kind):
    shader = GL.glCreateShader(kind)
    GL.glShaderSource(shader, source)
    GL.glCompileShader(shader)
    if not GL.glGetShaderiv(shader, GL.GL_COMPILE_STATUS):
        log = GL.glGetShaderInfoLog(shader).decode("utf-8", "replace")
        GL.glDeleteShader(shader)
        name = "vertex" if kind == GL.GL_VERTEX_SHADER else "fragment"
        raise GLError(f"{name} shader failed to compile:\n{log}")
    return shader


def compile_program(vert_src, frag_src):
    """Compile and link a shader program, returning its GL handle."""
    vert = _compile_shader(vert_src, GL.GL_VERTEX_SHADER)
    frag = _compile_shader(frag_src, GL.GL_FRAGMENT_SHADER)
    program = GL.glCreateProgram()
    GL.glAttachShader(program, vert)
    GL.glAttachShader(program, frag)
    GL.glLinkProgram(program)
    GL.glDeleteShader(vert)
    GL.glDeleteShader(frag)
    if not GL.glGetProgramiv(program, GL.GL_LINK_STATUS):
        log = GL.glGetProgramInfoLog(program).decode("utf-8", "replace")
        GL.glDeleteProgram(program)
        raise GLError(f"program failed to link:\n{log}")
    return program
