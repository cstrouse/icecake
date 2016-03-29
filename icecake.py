# -*- coding: utf8 -*-
"""
Icecake

This module provides a simple static site builder, similar to pelican or
octopress. It is intended to be small, light, and easy to modify. Out of the box
it supports the following features:

- Markdown formatting
- Pygments source code highlighting
- Jinja 2 templates
"""
from __future__ import absolute_import, division, print_function, unicode_literals
import platform
import logging
import os
from os.path import abspath, basename, dirname, exists, isdir, isfile, join, normpath, relpath, splitext
import time
import shutil


import click
import jinja2
import markdown
from dateutil.parser import parse as dateparse
from werkzeug.contrib.atom import AtomFeed
import watchdog.observers
import watchdog.events


from templates import templates
if platform.python_version_tuple()[0] == '2':
    import ConfigParser as configparser
    import io
else:
    import configparser


__metaclass__ = type
logging.basicConfig(level=logging.ERROR)


def ls_relative(list_path):
    """
    List files relative to the specified path
    """
    found = []
    # Guard against dir doesn't exist
    if not isdir(list_path):
        return found
    for path, dirs, files in os.walk(list_path):
        for file in files:
            filepath = join(path, file)
            found.append(relpath(filepath, list_path))
    found.sort()  # Make sure the sort order is deterministic
    return found


class ContentCache:
    def __init__(self, root):
        self.root = root
        self.files = {}
        self.pages = {}
        self.templates = {}

    def peek(self, filename):
        """
        peek when you want to get fresh data from disk but NOT store it in the cache
        """
        file = join(self.root, filename)
        if isfile(file):
            content = open(file).read()
            return content
        return None

    def read(self, filename):
        """
        read when you want to get fresh data from disk and store it in the cache
        """
        content = self.peek(filename)
        if content is not None:
            self.set(filename, content)
        return content

    def set(self, filename, content):
        if filename.startswith('content'):
            # Markdown files are not templates so let's skip those
            if splitext(filename)[1] != '.md':
                self.templates[relpath(filename, 'content')] = content
        if filename.startswith('layouts'):
            self.templates[relpath(filename, 'layouts')] = content
        self.files[filename] = content

    def get(self, filename):
        if filename in self.files:
            return self.files[filename]
        return None

    def delete(self, filename):
        del(self.files[filename])

    def move(self, old, new):
        if old not in self.files:
            return
        self.set(new, self.get(old))
        self.delete(old)

    def warm(self):
        for path in ['content', 'layouts']:
            for file in ls_relative(join(self.root, path)):
                self.read(join(path, file))


class Page:
    """
    A page is any discrete piece of content that will appear in your output
    folder. At minimum a page should have a body, title, and slug so it can be
    rendered. However, a page may have additional metadata like date or tags.
    """
    metadata = ["tags", "date", "title", "slug", "template"]
    required = ["date", "title"]
    metadelimiter = "++++"

    def __init__(self, filepath, site):
        # These are set when the page is initialized (step 1)
        self.parsed = False  # This is a special flag that helps us avoid bugs
        self.site = site
        self.abspath = abspath(filepath)
        # This is the path of the file relative to content
        self.filepath = relpath(filepath, join(site.root, 'content'))
        self.folder = self._get_folder()      # This is the leading part of the URL
        self.slug = self._get_default_slug()  # This is the final part of the URL
        self.ext = self._get_extension()      # This is the extension (markdown or html)

        # Note! We are going to evaluate this now to make sure it is always
        # evaluated. However, we will evaluate this again after the metadata is
        # parsed in case the user specifies a custom slug, which will change the
        # url. This way, though, we always have url defined even for pages
        # without metadata.
        self.url = self._get_url()           # This is the URL path

        # These maybe set when the page is parsed for metadata (step 2)
        self.date = None      # This is the date the page was published
        self.tags = None      # This is a list of tags, used to build links
        self.template = None  # This is the template we'll use to render the page
        self.title = None     # This is the title of the page

        # These are set when the page is rendered (step 3)
        self.body = None      # This is the raw body of the page
        self.content = None   # This is the content string for markdown pages
        self.rendered = None  # This is the HTML content of the page

    def _get_folder(self):
        return dirname(self.filepath)

    def _get_default_slug(self):
        """
        Get the slug for this page, which is used for the final part of the URL
        path. For a file like cakes/chocolate.html this will be "chocolate". If
        the slug is "index" we special case to "" so index.md and index.html can
        be used as you would expect without going into a folder called "index".
        If you want a folder called "index" you can specify the slug manually.
        """
        slug = splitext(basename(self.filepath))[0]
        if slug == "index":
            slug = ""
        return slug

    def _get_extension(self):
        """
        This is the file's extention, which is primarily used to identify
        markdown files.
        """
        return splitext(self.filepath)[1]

    def _get_url(self):
        """
        Get the url for this page, based on its filepath. This is path + slug.
        Something like cakes/chocolate.html will become /cakes/chocolate/
        """
        if self.ext in [".html", ".md", ".markdown"]:
            return '/%s/' % normpath(join(self.folder, self.slug))
        return '/%s' % join(self.folder, self.slug + self.ext)

    def get_target(self):
        """
        Get the target filename for this page. This is the containing folder +
        slug, which may have been customized.
        """
        # Note that we may have customized slug so we should only run this after
        # metadata has been parsed.
        if not self.parsed:
            raise RuntimeError("This should not be called before metadata is parsed")
        # If we find a .html or .md we will use .html instead, and will convert
        # the name to a folder containing index.html so we get a clean URL.
        if self.ext in [".html", ".md", ".markdown"]:
            return normpath(join(self.folder, self.slug, "index.html"))
        # We default to path/name.extension in case there is a file like CSS or
        # XML where the filename is important.
        return normpath(join(self.folder, self.slug + self.ext))

    def parse_metadata(self, text):
        """
        Parse a metadata string into object properties tags, date, title, etc.
        """
        logging.debug("Parsing metadata %s", text)
        text = "[Metadata]\n" + text
        parser = configparser.ConfigParser()
        if platform.python_version_tuple()[0] == '2':
            parser.readfp(io.StringIO(text))
        else:
            parser.read_string(text)

        # This is super ugly because of 2/3 compat. There's probably a cleaner
        # way to factor this code.
        values = {}
        for k, v in parser.items("Metadata"):
            values[k] = v
        for key in self.metadata:
            value = None
            if key in values:
                value = values[key]
            if key == "tags":
                if value is not None:
                    value = value.split(" ")
                else:
                    value = []
            # If we have a default metadata value and the user did not override
            # it, leave it alone. Currently this only applies to slug, which has
            # a default based on the filepath.
            if value is None and getattr(self, key, None) is not None:
                continue
            setattr(self, key, value)
            if value is None and key in self.required:
                logging.warning("Metadata '%s' not specified in %s", key,
                                self.filepath)
        self.url = self._get_url()
        self.parsed = True

    def render(self):
        """
        Render the page. All files will be rendered using Jinja. Markdown files
        with .md or .markdown extension will also use the markdown renderer. By
        default the base template is markdown.html for markdown and basic.html
        for everything else. Customize this via the "template" metadata field.
        """
        logging.debug("Rendering %s" % self.filepath)
        if self.ext in [".md", ".markdown"]:
            self.content = markdown.markdown(self.body, extensions=self.site.markdown_plugins)
            if self.template is not None:
                template = self.site.renderer.get_template(self.template)
            else:
                template = self.site.renderer.get_template("markdown.html")
        else:
            template = self.site.renderer.get_template(self.filepath)
        self.rendered = template.render(self.__dict__, site=self.site)
        return self.rendered

    def render_to_disk(self):
        output = self.render()
        target = join(self.site.root, 'output', self.filepath)
        target_dir = dirname(target)
        if not isdir(target_dir):
            os.makedirs(target_dir)
        file = open(target, mode='w')
        file.write(output)
        file.close()

    @classmethod
    def parse_string(cls, filepath, site, text):
        """
        Parse a raw string and separate the front matter so we can turn it into
        a page object with metadata and body.
        """
        page = cls(filepath, site)
        parts = text.split(cls.metadelimiter, 1)

        if len(parts) == 2:
            page.parse_metadata(parts[0].strip())
            page.body = parts[1].strip()
        else:
            page.body = parts[0].strip()
            if page.ext in ['.md', '.markdown']:
                logging.warning("No metadata detected; expected %s separator %s",
                                cls.metadelimiter, page.filepath)
            # Say we already parsed the metadata
            page.parsed = True
        return page

    @classmethod
    def parse_file(cls, filepath, site):
        """
        Read a file and return the Page created by passing it into parse_string
        """
        content = open(filepath).read()
        page = cls.parse_string(filepath, site, content)
        return page


class Site:
    """
    A site represents a collection of source files arranged under the following
    project structure:

    ├── content
    ├── layouts
    ├── static
    └── output

    Content represent pages on your site. Each page will be built into a
    corresponding file in your output directory. Layout files are used for
    shared templates but do not have a corresponding page in output. Files under
    static are copied directly, and is a good place to put images, css, and
    javascript.

    Methods on this class deal with configuration, discovering content and
    building your site.
    """

    def __init__(self, root):
        """
        Keyword Arguments:
        root -- The path to the static site folder which includes the pages,
                layouts, and static folders.
        """
        self.root = abspath(root)
        self.cache = ContentCache(root)
        self.cache.warm()
        self.markdown_plugins = ["markdown.extensions.fenced_code", "markdown.extensions.codehilite"]
        self.renderer = jinja2.Environment(loader=jinja2.DictLoader(self.cache.templates))
        self.pagedata = []

    def get_pages(self):
        """
        Enumerate and parse all the page files in the static site.
        """
        logging.debug("Getting pages")
        for file in self.cache.files:
            if not file.startswith('content'):
                continue
            source_file = join(self.root, file)
            if isfile(source_file):
                logging.debug("Parsing %s", source_file)
                page = Page.parse_file(source_file, self)
                page.render()
                self.pagedata.append(page)
        return self.pagedata

    def copy_static(self):
        logging.debug("Copying static files")
        static_dir = abspath(join(self.root, "static"))
        entries = os.walk(static_dir)
        for path, dirs, files in entries:
            for file in files:
                source_path = join(path, file)
                source_file = "." + source_path.replace(static_dir, "")
                output_file = normpath(join(self.root, "output", source_file))
                if isdir(source_path):
                    continue
                logging.debug("Copying static file %s", output_file)
                output_path = normpath(dirname(output_file))

                if not exists(output_path):
                    logging.debug("Making directory %s", output_path)
                    os.makedirs(output_path, mode=0o755)
                contents = open(source_path, "rb").read()
                open(output_file, "wb").write(contents)

    def build(self):
        """
        Build the site. This method originates all of the calls to discover,
        render, and place pages in the output directory. If you want to
        customize how your site is built, this is a good place to start.
        """
        self.pagedata = self.get_pages()
        for page in self.pagedata:
            output_filename = join(self.root, "output", page.get_target())
            logging.debug("Preparing to write %s", output_filename)
            output_path = dirname(output_filename)
            if not exists(output_path):
                logging.debug("Making directory %s", output_path)
                os.makedirs(output_path, mode=0o755)

            open(output_filename, mode="w").write(page.render())
            logging.info('Wrote %s', page.filepath)

        self.copy_static()

    def tags(self):
        tagnames = set()
        for page in self.pagedata:
            if page.tags:
                tagnames = tagnames.union(set(page.tags))
        taglist = list(tagnames)
        taglist.sort()
        return taglist

    def pages(self, path=None, tag=None, limit=None, order=None):
        """
        Filter the pages on your site by path or tag, and (optionally) sort or
        limit the number of results. Path pages uses startswith. Tag pages
        uses exact match. Use -ORDER for reverse sort. Examples:

        finder.pages(path="articles", limit=5, order="-date")
        finder.pages(tag="family", order="title")
        """
        items = self.pagedata
        if path is not None:
            items = [page for page in items if page.filepath.startswith(path)]
        if tag is not None:
            items = [page for page in items if tag in page.tags]
        if order is not None:
            rev = False
            if order[0] == "-":
                rev = True
                order = order[1:]
            items.sort(key=lambda x: getattr(x, order), reverse=rev)
        if limit is not None and limit > 0:
            items = items[:limit]
        return items

    def atom(self, feed_title, feed_url, feed_subtitle, site_url, author, *args, **kwargs):
        items = self.pages(*args, **kwargs)

        atom = AtomFeed(title=feed_title,
                        subtitle=feed_subtitle,
                        feed_url=feed_url,
                        url=site_url)
        for item in items:
            item.render()
            atom.add(title=item.title,
                     content=item.content,
                     content_type='html',
                     author=author,
                     url=site_url+item.url,
                     published=dateparse(item.date),
                     updated=dateparse(item.date),
                     xml_base=None)
        return atom.to_string()

    def clean_output(self):
        """
        Delete everything in the output folder so we can perform a clean build
        """
        shutil.rmtree(join(self.root, 'output'))

    @classmethod
    def initialize(cls, root):
        if not isdir(root):
            os.makedirs(root)
        for path, contents in templates.items():
            target = join(root, path)
            target_dir = dirname(target)
            if not isdir(target_dir):
                os.makedirs(target_dir)

            with open(target, mode="w") as f:
                logging.info("Writing %s" % target)
                f.write(contents)
                f.close()
        return Site(root)


class Handler(watchdog.events.FileSystemEventHandler):
    site = None

    def on_created(self, event):
        data = self.site.cache.read(event.src_path)
        page = Page.parse_string(event.src_path, self.site, data)
        page.render_to_disk()

    def on_deleted(self, event):
        if event.src_path.startswith(join(self.site.root, 'content')) or \
                event.src_path.startswith(join(self.site.root, 'layouts')):
            shutil.rmtree(event.src_path)

    def on_modified(self, event):
        # TODO also re-render any pages that depend on this one
        # Even though we need to reparse everything we may want to reuse the page
        # so we can more easily rebuild the dep graph / reference indexes
        logging.debug(event)
        path = relpath(event.src_path, self.site.root)
        logging.debug('rebuilding %s' % path)
        if self.site.cache.get(path) != self.site.cache.read(path):
            data = self.site.cache.get(path)
            page = Page.parse_string(path, self.site, data)
            page.render_to_disk()

    def on_moved(self, event):
        pass


class Watcher:
    def __init__(self, root):
        self.root = root
        self.site = Site(root)
        Handler.site = self.site

    def watch(self):
        obs = watchdog.observers.Observer()
        obs.schedule(Handler(), join(self.root, 'content'), recursive=True)
        obs.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            obs.stop()
        obs.join()


class Server:
    def __init__(self, root):
        self.root = root
        self.site = Site(root)

    def serve(self):
        pass


curdir = abspath(os.getcwd())


@click.group()
def cli():
    logging.basicConfig(level=logging.INFO)


@cli.command(help="""
    Initialize a project in the specified directory. The path will be created if
    it does not exist.
    """)
@click.option("--debug/--no-debug", default=False)
@click.option("-f/--force", help="Initialize even if the directory is not empty", default=False)
@click.argument("path", type=click.Path())
def init(debug, f, path):
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)
    if len(ls_relative(path)) > 0 and not f:
        click.echo("Path \"%s\" already contains files; use -f to force initialization" % path)
        exit(1)
    Site.initialize(path)


@cli.command()
@click.option("--debug/--no-debug", default=False)
def build(debug):
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)
    Site(curdir).build()


@cli.command()
@click.option("--debug/--no-debug", default=False)
def preview(debug):
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)
    Server(curdir).serve()


@cli.command()
@click.option("--debug/--no-debug", default=False)
def watch(debug):
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)
    Watcher(curdir).watch()


if __name__ == "__main__":
    cli()
