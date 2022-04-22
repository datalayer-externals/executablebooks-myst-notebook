"""An extension for sphinx"""
from __future__ import annotations

from collections import defaultdict
from contextlib import suppress
from importlib import resources as import_resources
import json
import os
from pathlib import Path
from typing import Any, DefaultDict, cast

from docutils import nodes
from markdown_it.token import Token
from markdown_it.tree import SyntaxTreeNode
from myst_parser import setup_sphinx as setup_myst_parser
from myst_parser.docutils_renderer import token_line
from myst_parser.main import MdParserConfig, create_md_parser
from myst_parser.sphinx_parser import MystParser
from myst_parser.sphinx_renderer import SphinxRenderer
import nbformat
from sphinx.application import Sphinx
from sphinx.environment import BuildEnvironment
from sphinx.environment.collectors import EnvironmentCollector
from sphinx.transforms.post_transforms import SphinxPostTransform
from sphinx.util import logging as sphinx_logging
from sphinx.util.fileutil import copy_asset_file

from myst_nb import __version__, static
from myst_nb._compat import findall
from myst_nb.core.config import NbParserConfig
from myst_nb.core.execute import ExecutionResult, execute_notebook
from myst_nb.core.loggers import DEFAULT_LOG_TYPE, SphinxDocLogger
from myst_nb.core.parse import nb_node_to_dict, notebook_to_tokens
from myst_nb.core.preprocess import preprocess_notebook
from myst_nb.core.read import UnexpectedCellDirective, create_nb_reader
from myst_nb.core.render import (
    MimeData,
    NbElementRenderer,
    create_figure_context,
    get_mime_priority,
    load_renderer,
)
from myst_nb.ext.download import NbDownloadRole
from myst_nb.glue.crossref import ReplacePendingGlueReferences
from myst_nb.glue.domain import NbGlueDomain

SPHINX_LOGGER = sphinx_logging.getLogger(__name__)
OUTPUT_FOLDER = "jupyter_execute"

# used for deprecated config values,
# so we can tell if they have been set by a user, and warn them
UNSET = "--unset--"


class SphinxEnvType(BuildEnvironment):
    """Sphinx build environment, including attributes set by myst_nb."""

    myst_config: MdParserConfig
    mystnb_config: NbParserConfig
    nb_metadata: DefaultDict[str, dict]
    nb_new_exec_data: bool


def sphinx_setup(app: Sphinx):
    """Initialize Sphinx extension."""
    # note, for core events overview, see:
    # https://www.sphinx-doc.org/en/master/extdev/appapi.html#sphinx-core-events

    # Add myst-parser configuration and transforms (but does not add the parser)
    setup_myst_parser(app)

    # add myst-nb configuration variables
    for name, default, field in NbParserConfig().as_triple():
        if not field.metadata.get("sphinx_exclude"):
            # TODO add types?
            app.add_config_value(f"nb_{name}", default, "env", Any)
            if "legacy_name" in field.metadata:
                app.add_config_value(
                    f"{field.metadata['legacy_name']}", UNSET, "env", Any
                )
    # Handle non-standard deprecation
    app.add_config_value("nb_render_priority", UNSET, "env", Any)

    # generate notebook configuration from Sphinx configuration
    # this also validates the configuration values
    app.connect("builder-inited", create_mystnb_config)

    # add parser and default associated file suffixes
    app.add_source_parser(Parser)
    app.add_source_suffix(".md", "myst-nb", override=True)
    app.add_source_suffix(".ipynb", "myst-nb")
    # add additional file suffixes for parsing
    app.connect("config-inited", add_nb_custom_formats)
    # ensure notebook checkpoints are excluded from parsing
    app.connect("config-inited", add_exclude_patterns)
    # add collector for myst nb specific data
    app.add_env_collector(NbMetadataCollector)

    # TODO add an event which, if any files have been removed,
    # all jupyter-cache stage records with a non-existent path are removed
    # (just to keep it "tidy", but won't affect run)

    # add directive to ensure all notebook cells are converted
    app.add_directive("code-cell", UnexpectedCellDirective, override=True)
    app.add_directive("raw-cell", UnexpectedCellDirective, override=True)

    # add directive for downloading an executed notebook
    app.add_role("nb-download", NbDownloadRole())

    # add post-transform for selecting mime type from a bundle
    app.add_post_transform(SelectMimeType)
    app.add_post_transform(ReplacePendingGlueReferences)

    # add HTML resources
    app.add_css_file("mystnb.css")
    app.connect("build-finished", add_global_html_resources)
    # note, this event is only available in Sphinx >= 3.5
    app.connect("html-page-context", add_per_page_html_resources)

    # add configuration for hiding cell input/output
    # TODO replace this, or make it optional
    app.setup_extension("sphinx_togglebutton")
    app.connect("config-inited", update_togglebutton_classes)

    # Note lexers are registered as `pygments.lexers` entry-points
    # and so do not need to be added here.

    # setup extension for execution statistics tables
    # import here, to avoid circular import
    from myst_nb.ext.execution_tables import setup_exec_table_extension

    setup_exec_table_extension(app)

    # add glue roles and directives
    # note, we have to add this as a domain, to allow for ':' in the names,
    # without a sphinx warning
    app.add_domain(NbGlueDomain)

    return {
        "version": __version__,
        "parallel_read_safe": True,
        "parallel_write_safe": True,
    }


def add_nb_custom_formats(app: Sphinx, config):
    """Add custom conversion formats."""
    for suffix in config.nb_custom_formats:
        app.add_source_suffix(suffix, "myst-nb", override=True)


def create_mystnb_config(app):
    """Generate notebook configuration from Sphinx configuration"""

    # Ignore type checkers because the attribute is dynamically assigned
    from sphinx.util.console import bold  # type: ignore[attr-defined]

    values = {}
    for name, _, field in NbParserConfig().as_triple():
        if not field.metadata.get("sphinx_exclude"):
            values[name] = app.config[f"nb_{name}"]
            if "legacy_name" in field.metadata:
                legacy_value = app.config[field.metadata["legacy_name"]]
                if legacy_value != UNSET:
                    legacy_name = field.metadata["legacy_name"]
                    SPHINX_LOGGER.warning(
                        f"{legacy_name!r} is deprecated for 'nb_{name}' "
                        f"[{DEFAULT_LOG_TYPE}.config]",
                        type=DEFAULT_LOG_TYPE,
                        subtype="config",
                    )
                    values[name] = legacy_value
    if app.config["nb_render_priority"] != UNSET:
        SPHINX_LOGGER.warning(
            "'nb_render_priority' is deprecated for 'nb_mime_priority_overrides'"
            f"{DEFAULT_LOG_TYPE}.config",
            type=DEFAULT_LOG_TYPE,
            subtype="config",
        )

    try:
        app.env.mystnb_config = NbParserConfig(**values)
        SPHINX_LOGGER.info(
            bold("myst-nb v%s:") + " %s", __version__, app.env.mystnb_config
        )
    except (TypeError, ValueError) as error:
        SPHINX_LOGGER.critical("myst-nb configuration invalid: %s", error.args[0])
        raise

    # update the output_folder (for writing external files like images),
    # and the execution_cache_path (for caching notebook outputs)
    # to a set path within the sphinx build folder
    output_folder = Path(app.outdir).parent.joinpath(OUTPUT_FOLDER).resolve()
    exec_cache_path: None | str | Path = app.env.mystnb_config.execution_cache_path
    if not exec_cache_path:
        exec_cache_path = Path(app.outdir).parent.joinpath(".jupyter_cache").resolve()
    app.env.mystnb_config = app.env.mystnb_config.copy(
        output_folder=str(output_folder), execution_cache_path=str(exec_cache_path)
    )
    SPHINX_LOGGER.info(f"Using jupyter-cache at: {exec_cache_path}")


def add_exclude_patterns(app: Sphinx, config):
    """Add default exclude patterns (if not already present)."""
    if "**.ipynb_checkpoints" not in config.exclude_patterns:
        config.exclude_patterns.append("**.ipynb_checkpoints")


def add_global_html_resources(app: Sphinx, exception):
    """Add HTML resources that apply to all pages."""
    # see https://github.com/sphinx-doc/sphinx/issues/1379
    if app.builder is not None and app.builder.format == "html" and not exception:
        with import_resources.path(static, "mystnb.css") as source_path:
            destination = os.path.join(app.builder.outdir, "_static", "mystnb.css")
            copy_asset_file(str(source_path), destination)


def add_per_page_html_resources(
    app: Sphinx, pagename: str, *args: Any, **kwargs: Any
) -> None:
    """Add JS files for this page, identified from the parsing of the notebook."""
    if app.env is None or app.builder is None or app.builder.format != "html":
        return
    js_files = NbMetadataCollector.get_js_files(app.env, pagename)  # type: ignore
    for path, kwargs in js_files.values():
        app.add_js_file(path, **kwargs)  # type: ignore


def update_togglebutton_classes(app: Sphinx, config):
    """Update togglebutton classes to recognise hidden cell inputs/outputs."""
    to_add = [
        ".tag_hide_input div.cell_input",
        ".tag_hide-input div.cell_input",
        ".tag_hide_output div.cell_output",
        ".tag_hide-output div.cell_output",
        ".tag_hide_cell.cell",
        ".tag_hide-cell.cell",
    ]
    for selector in to_add:
        config.togglebutton_selector += f", {selector}"


class Parser(MystParser):
    """Sphinx parser for Jupyter Notebook formats, containing MyST Markdown."""

    supported = ("myst-nb",)
    translate_section_name = None

    config_section = "myst-nb parser"
    config_section_dependencies = ("parsers",)

    def parse(self, inputstring: str, document: nodes.document) -> None:
        """Parse source text.

        :param inputstring: The source string to parse
        :param document: The root docutils node to add AST elements to
        """
        assert self.env is not None, "env not set"
        self.env: SphinxEnvType
        document_path = self.env.doc2path(self.env.docname)

        # get a logger for this document
        logger = SphinxDocLogger(document)

        # get markdown parsing configuration
        md_config: MdParserConfig = self.env.myst_config
        # get notebook rendering configuration
        nb_config: NbParserConfig = self.env.mystnb_config

        # create a reader for the notebook
        nb_reader = create_nb_reader(document_path, md_config, nb_config, inputstring)
        # If the nb_reader is None, then we default to a standard Markdown parser
        if nb_reader is None:
            return super().parse(inputstring, document)
        notebook = nb_reader.read(inputstring)

        # Update mystnb configuration with notebook level metadata
        if nb_config.metadata_key in notebook.metadata:
            overrides = nb_node_to_dict(notebook.metadata[nb_config.metadata_key])
            overrides.pop("output_folder", None)  # this should not be overridden
            try:
                nb_config = nb_config.copy(**overrides)
            except Exception as exc:
                logger.warning(
                    f"Failed to update configuration with notebook metadata: {exc}",
                    subtype="config",
                )
            else:
                logger.debug(
                    "Updated configuration with notebook metadata", subtype="config"
                )

        # potentially execute notebook and/or populate outputs from cache
        notebook, exec_data = execute_notebook(
            notebook, document_path, nb_config, logger, nb_reader.read_fmt
        )
        if exec_data:
            NbMetadataCollector.set_exec_data(self.env, self.env.docname, exec_data)
            if exec_data["traceback"]:
                # store error traceback in outdir and log its path
                reports_file = Path(self.env.app.outdir).joinpath(
                    "reports", *(self.env.docname + ".err.log").split("/")
                )
                reports_file.parent.mkdir(parents=True, exist_ok=True)
                reports_file.write_text(exec_data["traceback"], encoding="utf8")
                logger.warning(
                    f"Notebook exception traceback saved in: {reports_file}",
                    subtype="exec",
                )

        # Setup the parser
        mdit_parser = create_md_parser(nb_reader.md_config, SphinxNbRenderer)
        mdit_parser.options["document"] = document
        mdit_parser.options["notebook"] = notebook
        mdit_parser.options["nb_config"] = nb_config
        mdit_renderer: SphinxNbRenderer = mdit_parser.renderer  # type: ignore
        mdit_env: dict[str, Any] = {}

        # load notebook element renderer class from entry-point name
        # this is separate from SphinxNbRenderer, so that users can override it
        renderer_name = nb_config.render_plugin
        nb_renderer: NbElementRenderer = load_renderer(renderer_name)(
            mdit_renderer, logger
        )
        # we temporarily store nb_renderer on the document,
        # so that roles/directives can access it
        document.attributes["nb_renderer"] = nb_renderer
        # we currently do this early, so that the nb_renderer has access to things
        mdit_renderer.setup_render(mdit_parser.options, mdit_env)

        # pre-process notebook and store resources for render
        resources = preprocess_notebook(
            notebook, logger, mdit_renderer.get_cell_render_config
        )
        mdit_renderer.md_options["nb_resources"] = resources

        # parse to tokens
        mdit_tokens = notebook_to_tokens(notebook, mdit_parser, mdit_env, logger)
        # convert to docutils AST, which is added to the document
        mdit_renderer.render(mdit_tokens, mdit_parser.options, mdit_env)

        # write final (updated) notebook to output folder (utf8 is standard encoding)
        path = self.env.docname.split("/")
        ipynb_path = path[:-1] + [path[-1] + ".ipynb"]
        content = nbformat.writes(notebook).encode("utf-8")
        nb_renderer.write_file(ipynb_path, content, overwrite=True)

        # write glue data to the output folder,
        # and store the keys to environment doc metadata,
        # so that they may be used in any post-transform steps
        if resources.get("glue", None):
            glue_path = path[:-1] + [path[-1] + ".glue.json"]
            nb_renderer.write_file(
                glue_path,
                json.dumps(resources["glue"], cls=BytesEncoder).encode("utf8"),
                overwrite=True,
            )
            NbMetadataCollector.set_doc_data(
                self.env, self.env.docname, "glue", list(resources["glue"].keys())
            )

        # move some document metadata to environment metadata,
        # so that we can later read it from the environment,
        # rather than having to load the whole doctree
        for key, (uri, kwargs) in document.attributes.pop("nb_js_files", {}).items():
            NbMetadataCollector.add_js_file(
                self.env, self.env.docname, key, uri, kwargs
            )

        # remove temporary state
        document.attributes.pop("nb_renderer")


class SphinxNbRenderer(SphinxRenderer):
    """A sphinx renderer for Jupyter Notebooks."""

    @property
    def nb_config(self) -> NbParserConfig:
        """Get the notebook element renderer."""
        return self.md_options["nb_config"]

    @property
    def nb_renderer(self) -> NbElementRenderer:
        """Get the notebook element renderer."""
        return self.document["nb_renderer"]

    def get_cell_render_config(
        self,
        cell_metadata: dict[str, Any],
        key: str,
        nb_key: str | None = None,
        has_nb_key: bool = True,
    ) -> Any:
        """Get a cell level render configuration value.

        :param has_nb_key: Whether to also look in the notebook level configuration
        :param nb_key: The notebook level configuration key to use if the cell
            level key is not found. if None, use the ``key`` argument

        :raises: IndexError if the cell index is out of range
        :raises: KeyError if the key is not found
        """
        # TODO allow output level configuration?
        cell_metadata_key = self.nb_config.cell_render_key
        if (
            cell_metadata_key not in cell_metadata
            or key not in cell_metadata[cell_metadata_key]
        ):
            if not has_nb_key:
                raise KeyError(key)
            return self.nb_config[nb_key if nb_key is not None else key]
        # TODO validate?
        return cell_metadata[cell_metadata_key][key]

    def render_nb_metadata(self, token: SyntaxTreeNode) -> None:
        """Render the notebook metadata."""
        env = cast(BuildEnvironment, self.sphinx_env)
        metadata = dict(token.meta)
        special_keys = ("kernelspec", "language_info", "source_map")
        for key in special_keys:
            if key in metadata:
                # save these special keys on the metadata, rather than as docinfo
                # note, sphinx_book_theme checks kernelspec is in the metadata
                env.metadata[env.docname][key] = metadata.get(key)

        metadata = self.nb_renderer.render_nb_metadata(metadata)

        # forward the remaining metadata to the front_matter renderer
        top_matter = {k: v for k, v in metadata.items() if k not in special_keys}
        self.render_front_matter(
            Token(  # type: ignore
                "front_matter",
                "",
                0,
                map=[0, 0],
                content=top_matter,  # type: ignore[arg-type]
            ),
        )

    def render_nb_cell_markdown(self, token: SyntaxTreeNode) -> None:
        """Render a notebook markdown cell."""
        # TODO this is currently just a "pass-through", but we could utilise the metadata
        # it would be nice to "wrap" this in a container that included the metadata,
        # but unfortunately this would break the heading structure of docutils/sphinx.
        # perhaps we add an "invisible" (non-rendered) marker node to the document tree,
        self.render_children(token)

    def render_nb_cell_raw(self, token: SyntaxTreeNode) -> None:
        """Render a notebook raw cell."""
        line = token_line(token, 0)
        _nodes = self.nb_renderer.render_raw_cell(
            token.content, token.meta["metadata"], token.meta["index"], line
        )
        self.add_line_and_source_path_r(_nodes, token)
        self.current_node.extend(_nodes)

    def render_nb_cell_code(self, token: SyntaxTreeNode) -> None:
        """Render a notebook code cell."""
        cell_index = token.meta["index"]
        tags = token.meta["metadata"].get("tags", [])

        # TODO do we need this -/_ duplication of tag names, or can we deprecate one?
        remove_input = (
            self.get_cell_render_config(token.meta["metadata"], "remove_code_source")
            or ("remove_input" in tags)
            or ("remove-input" in tags)
        )
        remove_output = (
            self.get_cell_render_config(token.meta["metadata"], "remove_code_outputs")
            or ("remove_output" in tags)
            or ("remove-output" in tags)
        )

        # if we are remove both the input and output, we can skip the cell
        if remove_input and remove_output:
            return

        # create a container for all the input/output
        classes = ["cell"]
        for tag in tags:
            classes.append(f"tag_{tag.replace(' ', '_')}")
        cell_container = nodes.container(
            nb_element="cell_code",
            cell_index=cell_index,
            # TODO some way to use this to allow repr of count in outputs like HTML?
            exec_count=token.meta["execution_count"],
            cell_metadata=token.meta["metadata"],
            classes=classes,
        )
        self.add_line_and_source_path(cell_container, token)
        with self.current_node_context(cell_container, append=True):

            # render the code source code
            if not remove_input:
                cell_input = nodes.container(
                    nb_element="cell_code_source", classes=["cell_input"]
                )
                self.add_line_and_source_path(cell_input, token)
                with self.current_node_context(cell_input, append=True):
                    self.render_nb_cell_code_source(token)

            # render the execution output, if any
            has_outputs = self.md_options["notebook"]["cells"][cell_index].get(
                "outputs", []
            )
            if (not remove_output) and has_outputs:
                cell_output = nodes.container(
                    nb_element="cell_code_output", classes=["cell_output"]
                )
                self.add_line_and_source_path(cell_output, token)
                with self.current_node_context(cell_output, append=True):
                    self.render_nb_cell_code_outputs(token)

    def render_nb_cell_code_source(self, token: SyntaxTreeNode) -> None:
        """Render a notebook code cell's source."""
        # cell_index = token.meta["index"]
        lexer = token.meta.get("lexer", None)
        node = self.create_highlighted_code_block(
            token.content,
            lexer,
            number_lines=self.get_cell_render_config(
                token.meta["metadata"], "number_source_lines"
            ),
            source=self.document["source"],
            line=token_line(token),
        )
        self.add_line_and_source_path(node, token)
        self.current_node.append(node)

    def render_nb_cell_code_outputs(self, token: SyntaxTreeNode) -> None:
        """Render a notebook code cell's outputs."""
        line = token_line(token, 0)
        cell_index = token.meta["index"]
        metadata = token.meta["metadata"]
        outputs: list[nbformat.NotebookNode] = self.md_options["notebook"]["cells"][
            cell_index
        ].get("outputs", [])
        # render the outputs
        for output_index, output in enumerate(outputs):
            if output.output_type == "stream":
                if output.name == "stdout":
                    _nodes = self.nb_renderer.render_stdout(
                        output, metadata, cell_index, line
                    )
                    self.add_line_and_source_path_r(_nodes, token)
                    self.current_node.extend(_nodes)
                elif output.name == "stderr":
                    _nodes = self.nb_renderer.render_stderr(
                        output, metadata, cell_index, line
                    )
                    self.add_line_and_source_path_r(_nodes, token)
                    self.current_node.extend(_nodes)
                else:
                    pass  # TODO warning
            elif output.output_type == "error":
                _nodes = self.nb_renderer.render_error(
                    output, metadata, cell_index, line
                )
                self.add_line_and_source_path_r(_nodes, token)
                self.current_node.extend(_nodes)
            elif output.output_type in ("display_data", "execute_result"):

                # Note, this is different to the docutils implementation,
                # where we directly select a single output, based on the mime_priority.
                # Here, we do not know the mime priority until we know the output format
                # so we output all the outputs during this parsing phase
                # (this is what sphinx caches as "output format agnostic" AST),
                # and replace the mime_bundle with the format specific output
                # in a post-transform (run per output format on the cached AST)

                # TODO how to output MyST Markdown?
                # currently text/markdown is set to be rendered as CommonMark only,
                # with headings dissallowed,
                # to avoid "side effects" if the mime is discarded but contained
                # targets, etc, and because we can't parse headings within containers.
                # perhaps we could have a config option to allow this?
                # - for non-commonmark, the text/markdown would always be considered
                #   the top priority, and all other mime types would be ignored.
                # - for headings, we would also need to parsing the markdown
                #   at the "top-level", i.e. not nested in container(s)

                figure_options = None
                with suppress(KeyError):
                    figure_options = self.get_cell_render_config(
                        metadata, "figure", has_nb_key=False
                    )

                with create_figure_context(self, figure_options, line):
                    mime_bundle = nodes.container(nb_element="mime_bundle")
                    with self.current_node_context(mime_bundle):
                        for mime_type, data in output["data"].items():
                            mime_container = nodes.container(mime_type=mime_type)
                            with self.current_node_context(mime_container):
                                _nodes = self.nb_renderer.render_mime_type(
                                    MimeData(
                                        mime_type,
                                        data,
                                        cell_metadata=metadata,
                                        output_metadata=output.get("metadata", {}),
                                        cell_index=cell_index,
                                        output_index=output_index,
                                        line=line,
                                    )
                                )
                                self.current_node.extend(_nodes)
                            if mime_container.children:
                                self.current_node.append(mime_container)
                    if mime_bundle.children:
                        self.add_line_and_source_path_r([mime_bundle], token)
                        self.current_node.append(mime_bundle)
            else:
                self.create_warning(
                    f"Unsupported output type: {output.output_type}",
                    line=line,
                    append_to=self.current_node,
                    wtype=DEFAULT_LOG_TYPE,
                    subtype="output_type",
                )


class SelectMimeType(SphinxPostTransform):
    """Select the mime type to render from mime bundles,
    based on the builder and its associated priority list.
    """

    default_priority = 4  # TODO set correct priority

    def run(self, **kwargs: Any) -> None:
        """Run the transform."""
        # get priority list for this builder
        # TODO allow for per-notebook/cell priority dicts?
        bname = self.app.builder.name  # type: ignore
        priority_list = get_mime_priority(
            bname, self.config["nb_mime_priority_overrides"]
        )
        condition = (
            lambda node: isinstance(node, nodes.container)
            and node.attributes.get("nb_element", "") == "mime_bundle"
        )
        # remove/replace_self will not work with an iterator
        for node in list(findall(self.document)(condition)):
            # get available mime types
            mime_types = [node["mime_type"] for node in node.children]
            if not mime_types:
                node.parent.remove(node)
                continue
            # select top priority
            index = None
            for mime_type in priority_list:
                try:
                    index = mime_types.index(mime_type)
                except ValueError:
                    continue
                else:
                    break
            if index is None:
                mime_string = ",".join(repr(m) for m in mime_types)
                SPHINX_LOGGER.warning(
                    f"No mime type available in priority list for builder {bname!r} "
                    f"({mime_string}) [{DEFAULT_LOG_TYPE}.mime_priority]",
                    type=DEFAULT_LOG_TYPE,
                    subtype="mime_priority",
                    location=node,
                )
                node.parent.remove(node)
            elif not node.children[index].children:
                node.parent.remove(node)
            else:
                node.replace_self(node.children[index].children)


class NbMetadataCollector(EnvironmentCollector):
    """Collect myst-nb specific metdata, and handle merging of parallel builds."""

    @staticmethod
    def set_doc_data(env: SphinxEnvType, docname: str, key: str, value: Any) -> None:
        """Add nb metadata for a docname to the environment."""
        if not hasattr(env, "nb_metadata"):
            env.nb_metadata = defaultdict(dict)
        env.nb_metadata.setdefault(docname, {})[key] = value

    @staticmethod
    def get_doc_data(env: SphinxEnvType) -> DefaultDict[str, dict]:
        """Get myst-nb docname -> metadata dict."""
        if not hasattr(env, "nb_metadata"):
            env.nb_metadata = defaultdict(dict)
        return env.nb_metadata

    @classmethod
    def set_exec_data(
        cls, env: SphinxEnvType, docname: str, value: ExecutionResult
    ) -> None:
        """Add nb metadata for a docname to the environment."""
        cls.set_doc_data(env, docname, "exec_data", value)
        # TODO this does not take account of cache data
        cls.note_exec_update(env)

    @classmethod
    def get_exec_data(cls, env: SphinxEnvType, docname: str) -> ExecutionResult | None:
        """Get myst-nb docname -> execution data."""
        return cls.get_doc_data(env)[docname].get("exec_data")

    def get_outdated_docs(  # type: ignore[override]
        self,
        app: Sphinx,
        env: SphinxEnvType,
        added: set[str],
        changed: set[str],
        removed: set[str],
    ) -> list[str]:
        # called before any docs are read
        env.nb_new_exec_data = False
        return []

    @staticmethod
    def note_exec_update(env: SphinxEnvType) -> None:
        """Note that a notebook has been executed."""
        env.nb_new_exec_data = True

    @staticmethod
    def new_exec_data(env: SphinxEnvType) -> bool:
        """Return whether any notebooks have updated execution data."""
        return getattr(env, "nb_new_exec_data", False)

    @classmethod
    def add_js_file(
        cls,
        env: SphinxEnvType,
        docname: str,
        key: str,
        uri: str | None,
        kwargs: dict[str, str],
    ):
        """Register a JavaScript file to include in the HTML output."""
        if not hasattr(env, "nb_metadata"):
            env.nb_metadata = defaultdict(dict)
        js_files = env.nb_metadata.setdefault(docname, {}).setdefault("js_files", {})
        # TODO handle whether overrides are allowed
        js_files[key] = (uri, kwargs)

    @classmethod
    def get_js_files(
        cls, env: SphinxEnvType, docname: str
    ) -> dict[str, tuple[str | None, dict[str, str]]]:
        """Get myst-nb docname -> execution data."""
        return cls.get_doc_data(env)[docname].get("js_files", {})

    def clear_doc(  # type: ignore[override]
        self,
        app: Sphinx,
        env: SphinxEnvType,
        docname: str,
    ) -> None:
        if not hasattr(env, "nb_metadata"):
            env.nb_metadata = defaultdict(dict)
        env.nb_metadata.pop(docname, None)

    def process_doc(self, app: Sphinx, doctree: nodes.document) -> None:
        pass

    def merge_other(  # type: ignore[override]
        self,
        app: Sphinx,
        env: SphinxEnvType,
        docnames: set[str],
        other: SphinxEnvType,
    ) -> None:
        if not hasattr(env, "nb_metadata"):
            env.nb_metadata = defaultdict(dict)
        other_metadata = getattr(other, "nb_metadata", defaultdict(dict))
        for docname in docnames:
            env.nb_metadata[docname] = other_metadata[docname]
        if other.nb_new_exec_data:
            env.nb_new_exec_data = True


class BytesEncoder(json.JSONEncoder):
    """A JSON encoder that accepts b64 (and other *ascii*) bytestrings."""

    def default(self, obj):
        if isinstance(obj, bytes):
            return obj.decode("ascii")
        return json.JSONEncoder.default(self, obj)
