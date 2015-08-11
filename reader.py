"""Scrape Kindle Cloud Reader for information regarding the Kindle Library and
current reading progress.
"""
from credential_mgr import JSONCredentialManager

import sys
import os
from getpass import getuser
from textwrap import dedent

from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver import Firefox, ActionChains
from selenium.common.exceptions import NoSuchElementException,\
                                        ElementNotVisibleException


API_SCRIPT_PATH = 'kindle_api.js'


class ConnectionError(Exception):
    """Indicate a problem with the internet connection
    """
    pass


def get_ff_profile_path():
    """Return a firefox profile path
    NOTE: OSX only
    """
    uname = getuser()
    if sys.platform == 'darwin':
        prof_dir = '/Users/%s/Library/Application Support/Firefox/Profiles' % uname
        profiles = [s for s in os.listdir(prof_dir) if '.default' in s]

        return os.path.join(prof_dir, profiles[0])
    else:
        #TODO
        raise NotImplementedError


class KindleBook(object):
    """A Kindle Book

    Args:
        asin: The "Amazon Standard Item Number" of the book. Essentially a
            UUID for Kindle books.
        title: The book title
        authors: A list of the book's authors. May be empty.
    """
    def __init__(self, asin, title, authors):
        self.asin = unicode(asin)
        self.title = unicode(title)
        self.authors = map(unicode, authors) if authors else []

    def __str__(self):
        if not self.authors:
            ret = u'"{}"'.format(self.title)
        elif len(self.authors) == 1:
            ret = u'"{}" by {}'.format(self.title, self.authors[0])
        elif len(self.authors) == 2:
            ret = u'"{}" by {} and {}'\
                    .format(self.title, self.authors[0], self.authors[1])
        else:
            ret = u'"{}" by {}, and {}'\
                    .format(self.title, u', '.join(self.authors[:-1]),
                            self.authors[-1])
        return ret.encode('utf8')

    def __repr__(self):
        author_str = u', '.join([u'"%s"' % author for author in self.authors])
        return u'Book(asin={}, title="{}", authors=[{}])'\
                .format(self.asin, self.title, author_str)\
                .encode('utf8')


class ReadingProgress(object):
    """A representation of how far the reader is through a book

    Args:
        positions: A 3-tuple (start_position, current_position, end_position)
        locs: A 3-tuple (start_location, current_location, end_location)
        page_nums (optional): A 3-tuple (start_page, current_page, end_page)

    Notes on Progress Formats:

    Page Numbers:
        The page number measurement directly corresponds to the page
        numbers in a physical copy of the book. In other words, the page
        number N reported by the Kindle should correspond to that same
        page N in a hard copy.

    Locations:
        According to (http://www.amazon.com/forum/kindle/Tx2S4K44LSXEWRI)
        and various other online discussions, a single 'location' is
        equivalent to 128 bytes of code (in the azw3 file format).

        For normal books, this ranges from 3-4 locations per page with a
        large font to ~16 locs/pg with a small font. However, book
        elements such as images or charts may require many more bytes and,
        thus, locations to represent.

        In spite of this extra noise, locations provide a more granular
        measurement of reading progress than page numbers.

        Additionally, locations are available on every Kindle title while
        page numbers are frequently absent from Kindle metadata.

    Positions:
        Positions are the representation used to represent reading progress in
        the Kindle service. As such, it is the most granular measure
        available. I was unable to find any documentation on their meaning but
        the formulae found in the code indicate the equivalence between
        positions and locations is something like 150 to 1.
    """
    def __init__(self, positions, locs, page_nums=None):
        self.positions = positions
        self.locs = locs
        self.page_nums = page_nums

    def has_page_progress(self):
        """Return whether page numbering is available in this object
        """
        return self.page_nums is not None

    def __str__(self):
        if self.has_page_progress():
            return 'Page %d of %d' % (self.page_nums[1], self.page_nums[2])
        else:
            return 'Location %d of %d' % (self.locs[1], self.locs[2])

    def __repr__(self):
        if self.has_page_progress():
            return 'ReadingProgress(Loc=(%d of %d), Page=(%d of %d))' % \
                    (self.locs[1], self.locs[2],
                            self.page_nums[1], self.page_nums[2])
        else:
            return 'ReadingProgress(Loc=(%d of %d))' % \
                    (self.locs[1], self.locs[2])


class KindleCloudReaderAPI(object):
    """An interface for extracting data from Kindle Cloud Reader

    Args:
        amz_login_credentials_path: The system path to a JSON file containing
            two keys:
                id: The email address associated with the Kindle account
                secret: The password associated with the Kindle account
        profile_path: The path to the Firefox profile directory to use for
            browsing. This enables existing cookies and add-ons to be used in
            the automation.
    """
    CLOUD_READER_URL = u'https://read.amazon.com'
    SIGNIN_URL = u'https://www.amazon.com/ap/signin'

    def __init__(self, amz_login_credentials_path, profile_path=None):
        if profile_path is not None:
            profile = FirefoxProfile(profile_path)
        else:
            profile = None
        self._browser = Firefox(firefox_profile=profile)
        self._browser.set_script_timeout(10)
        self._wait = WebDriverWait(self._browser,
                timeout=10,
                ignored_exceptions=(NoSuchElementException,
                    ElementNotVisibleException))
        self._manager = JSONCredentialManager(amz_login_credentials_path)
        self._action = ActionChains(self._browser)
        with open(API_SCRIPT_PATH, 'r') as api_script_file:
            self._api_script = api_script_file.read()

    def _to_reader_home(self):
        """Navigate to the Cloud Reader library page
        """
        # NOTE: Prevents QueryInterface error caused by getting a URL
        # while switched to an iframe
        self._browser.switch_to_default_content()
        self._browser.get(KindleCloudReaderAPI.CLOUD_READER_URL)

        if self._browser.title == u'Problem loading page':
            raise ConnectionError

        # Wait for either the login page or the reader to load
        login_or_reader_loaded = \
                lambda br: br.find_elements_by_id('amzn_kcr') or \
                    br.find_elements_by_id('KindleLibraryIFrame')
        self._wait.until(login_or_reader_loaded)

        # If the login page was loaded, log in
        if self._browser.title == u'Amazon.com Sign In':
            self._login()

        assert self._browser.title == u'Kindle Cloud Reader'

    def _login(self):
        """Log in to Kindle Cloud Reader
        """
        if not self._browser.current_url.startswith(KindleCloudReaderAPI.SIGNIN_URL):
            raise RuntimeError('current url "%s" is not a signin url ("%s")' %
                    (self._browser.current_url, KindleCloudReaderAPI.SIGNIN_URL))
        uname, pword = self._manager.get_creds()
        self._browser.find_element_by_id('ap_email').send_keys(uname)
        self._browser.find_element_by_id('ap_password').send_keys(pword)
        self._browser.find_element_by_id('signInSubmit-input').click()

    def _switch_to_frame(self, frame_id):
        """Switch the browser focus to the iframe with id `frame_id`

        Args:
            frame_id: The id string attached to the frame
        """
        self._wait.until(lambda br: br.find_element_by_id(frame_id))
        self._browser.switch_to.frame(frame_id)  #pylint: disable=no-member

    def _get_api_call(self, function_name, *args):
        """Runs the api call `function_name` with the javascript-formatted
        arguments `*args`
        """
        self._switch_to_frame('KindleReaderIFrame')
        # Wait until the books have been loaded
        self._wait.until(lambda br: br.execute_async_script(
            ur"""
            var done = arguments[0];
            KindleModuleManager
                .isModuleInitialized(Kindle.MODULE.DB_CLIENT) &&
            KindleModuleManager
                .getModuleSync(Kindle.MODULE.DB_CLIENT)
                .getAppDb()
                .getAllBooks()
                .done(function(books) { done(!!books.length); });
            """))
        api_call = dedent("""
            var done = arguments[0];
            %(api_script)s
            KindleAPI.%(api_call)s(%(args)s).done(function(a) {
                done(a);
        });
        """) % {'api_script': self._api_script,
                'api_call': function_name,
                'args': ', '.join(args)
                }
        script = '\n'.join((self._api_script, api_call))
        return self._browser.execute_async_script(script)

    @staticmethod
    def _kbm_to_book(kbm):
        """Return a `KindleBook` instance from a dictionary representation of
        a javascript KindleBookMetadata object.
        """
        return KindleBook(**kbm)  #pylint: disable=star-args

    @staticmethod
    def _kbp_to_progress(kbp):
        """Return a `ReadingProgress` instance from a dictionary representation of
        a javascript KindleBookProgress object.
        """
        return ReadingProgress(**kbp)  #pylint: disable=star-args

    def get_book_metadata(self, asin):
        """Return metadata on the book with the ASIN supplied

        Returns:
            A `KindleBook` object
        """
        self._to_reader_home()
        kbm = self._get_api_call('get_book_metadata', asin)
        return KindleCloudReaderAPI._kbm_to_book(kbm)

    def get_library_metadata(self):
        """Return metadata on the books in the kindle library

        Returns:
            A list of `KindleBook` objects
        """
        self._to_reader_home()
        return map(KindleCloudReaderAPI._kbm_to_book,
                self._get_api_call('get_library_metadata'))

    def get_book_progress(self, asin):
        """Return a `ReadingProgress` object containing the available progress
        data.
        NOTE: A summary of the two progress formats can be found in the

        Args:
            read_cb: The callback to open the reader for the target book
        """
        self._to_reader_home()
        kbp = self._get_api_call('get_book_progress', asin)
        return KindleCloudReaderAPI._kbp_to_progress(kbp)

    def get_library_progress(self):
        """Return a dictionary mapping the `ReadingProgress`
        """
        self._to_reader_home()
        kbp_dict = self._get_api_call('get_library_progress')
        return {asin: KindleCloudReaderAPI._kbp_to_progress(kbp)
                for asin, kbp in kbp_dict.iteritems()}

    def close(self):
        """End the browser session
        """
        self._browser.quit()


if __name__ == "__main__":
    if sys.platform != 'darwin':
        raise RuntimeError('Non-OS X platforms not supported')

    CREDENTIAL_PATH = '.credentials.json'
    READER = KindleCloudReaderAPI(CREDENTIAL_PATH)
    print READER.get_library_metadata()
    print READER.get_library_progress()
    READER.close()
