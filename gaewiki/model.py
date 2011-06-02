# encoding=utf-8

import datetime
import logging
import random

from google.appengine.api import users
from google.appengine.ext import db

import settings
import util


class WikiUser(db.Model):
    wiki_user = db.UserProperty()
    joined = db.DateTimeProperty(auto_now_add=True)
    wiki_user_picture = db.BlobProperty()
    user_feed = db.StringProperty()
    nickname = db.StringProperty()
    public_email = db.StringProperty()

    def get_nickname(self):
        if self.nickname:
            return self.nickname
        return self.wiki_user.email().split('@', 1)[0]

    def get_public_email(self):
        return self.public_email or self.wiki_user.email()

    def put(self):
        if self.nickname:
            other = self.gql('WHERE nickname = :1', self.nickname).get()
            if other is not None and other.key() != self.key():
                raise RuntimeError('This nickname is already taken, please choose a different one.')
        return super(WikiUser, self).put()

    @classmethod
    def get_all(cls):
        return cls.all().order('wiki_user').fetch(1000)

    @classmethod
    def get_or_create(cls, user):
        if user is None:
            return None
        wiki_user = cls.gql('WHERE wiki_user = :1', user).get()
        if wiki_user is None:
            wiki_user = cls(wiki_user=user)
            wiki_user.nickname = cls.get_unique_nickname(wiki_user)
            wiki_user.put()
        return wiki_user

    @classmethod
    def get_unique_nickname(cls, user):
        nickname = user.get_nickname()
        while cls.gql('WHERE nickname = :1', nickname).get() is not None:
            nickname = user.get_nickname() + str(random.randrange(1111, 9999))
        return nickname


class WikiUserReference(db.ReferenceProperty):
    """For some reason db.ReferenceProperty itself fails to validate
    references, thinking that model.WikiUser != __main__.model.WikiUser,
    whatever that means.  Disabled until a solution is found.
    """
    def __init__(self):
        db.ReferenceProperty.__init__(self, WikiUser)

    def validate(self, value):
        return value


class WikiContent(db.Model):
    """Stores current versions of pages."""
    title = db.StringProperty(required=True)
    body = db.TextProperty(required=False)
    author = WikiUserReference()
    updated = db.DateTimeProperty(auto_now_add=True)
    created = db.DateTimeProperty(auto_now_add=True)
    pread = db.BooleanProperty()
    # The name of the page that this one redirects to.
    redirect = db.StringProperty()
    # Labels used by this page.
    labels = db.StringListProperty()
    # Pages that this one links to.
    links = db.StringListProperty()

    def put(self):
        """Adds the gaewiki:parent: labels transparently."""
        if self.body is not None:
            options = util.parse_page(self.body)
            self.redirect = options.get('redirect')
            self.pread = options.get('public') == 'yes' and options.get('private') != 'yes'
            self.labels = options.get('labels', [])

        self.add_implicit_labels()
        db.Model.put(self)
        settings.check_and_flush(self)

    def add_implicit_labels(self):
        labels = [l for l in self.labels if not l.startswith('gaewiki:parent:')]
        if '/' in self.title:
            parts = self.title.split('/')[:-1]
            while parts:
                label = 'gaewiki:parent:' + '/'.join(parts)
                labels.append(label)
                parts.pop()
                break # remove to add recursion
        self.labels = labels

    def backup(self):
        """Archives the current page revision."""
        logging.debug(u'Backing up page "%s"' % self.title)
        archive = WikiRevision(title=self.title, revision_body=self.body, author=self.author, created=self.updated)
        archive.put()

    def update(self, body, author, delete):
        if self.is_saved():
            self.backup()
            if delete:
                logging.debug(u'Deleting page "%s"' % self.title)
                self.delete()
                return

        logging.debug(u'Updating page "%s"' % self.title)

        self.body = body
        self.author = WikiUser.get_or_create(author)
        self.updated = datetime.datetime.now()

        # TODO: rename
        # TODO: cross-link

        self.put()

    def get_history(self):
        return WikiRevision.gql('WHERE title = :1 ORDER BY created DESC', self.title).fetch(100)

    def get_backlinks(self):
        return WikiContent.gql('WHERE links = :1', self.title).fetch(1000)

    def load_template(self, user, is_admin):
        template = '# PAGE_TITLE\n\n**PAGE_TITLE** is ...'
        template_names = ['gaewiki:anon page template']
        if user is not None:
            template_names.insert(0, 'gaewiki:user page template')
        if users.is_current_user_admin():
            template_names.insert(0, 'gaewiki:admin page template')
        for template_name in template_names:
            page = WikiContent.gql('WHERE title = :1', template_name).get()
            if page is not None:
                logging.debug('Loaded template from %s' % template_name)
                template = page.body.replace(template_name, 'PAGE_TITLE')
                break
        if user is not None:
            template = template.replace('USER_EMAIL', user.email())
        self.body = template.replace('PAGE_TITLE', self.title)

    @classmethod
    def get_by_title(cls, title, default_body=None):
        """Finds and loads the page by its title, creates a new one if nothing
        could be found."""
        title = title.replace('_', ' ')
        page = cls.gql('WHERE title = :1', title).get()
        if page is None:
            page = cls(title=title)
            if default_body is not None:
                page.body = default_body
        return page

    @classmethod
    def get_by_label(cls, label):
        """Returns a list of pages that have the specified label."""
        return cls.gql('WHERE labels = :1 ORDER BY title', label).fetch(100)

    @classmethod
    def get_publicly_readable(cls):
        if settings.get('open-reading') == 'yes':
            pages = cls.all()
        else:
            pages = cls.gql('WHERE pread = :1', True).fetch(1000)
        return sorted(pages, key=lambda p: p.title.lower())

    @classmethod
    def get_all(cls):
        return sorted(cls.all().order('title').fetch(1000), key=lambda p: p.title.lower())

    @classmethod
    def get_recently_added(cls, limit=100):
        return cls.all().order('-created').fetch(limit)

    @classmethod
    def get_changes(cls):
        if settings.get('open-reading') == 'yes':
            pages = cls.all().order('-updated').fetch(20)
        else:
            pages = cls.gql('WHERE pread = :1 ORDER BY -updated', True).fetch(20)
        return pages

    @classmethod
    def get_error_page(cls, error_code, default_body=None):
        return cls.get_by_title('gaewiki:error-%u' % error_code, default_body)


class WikiRevision(db.Model):
    """
    Stores older revisions of pages.
    """
    title = db.StringProperty()
    wiki_page = db.ReferenceProperty(WikiContent)
    revision_body = db.TextProperty(required=True)
    author = db.ReferenceProperty(WikiUser)
    created = db.DateTimeProperty(auto_now_add=True)
    pread = db.BooleanProperty()
