from redis import StrictRedis

from django.db import models
from django.contrib.sites.models import Site
from django.contrib.auth.models import User

from app_data import AppDataField

from ella.core.cache.fields import ContentTypeForeignKey, CachedGenericForeignKey, SiteForeignKey, CachedForeignKey
from ella.core.cache import get_cached_objects, get_cached_object, SKIP
from ella.utils import timezone

from ella_flatcomments.signals import comment_was_moderated, comment_will_be_posted, comment_was_posted
from ella_flatcomments.conf import comments_settings

redis = StrictRedis(**comments_settings.REDIS)

class CommentList(object):
    def __init__(self, content_type, object_id, reversed=False):
        self._key = comments_settings.LIST_KEY % (Site.objects.get_current().id, content_type.id, object_id)
        self._reversed = reversed

    def count(self):
        return redis.llen(self._key)
    __len__ = count

    def __getitem__(self, key):
        if isinstance(key, int):
            if self._reversed:
                key = -1 - key
            pk = redis.lindex(self._key, key)
            if pk is None:
                raise IndexError('list index out of range')
            return get_cached_object(FlatComment, pk=pk)

        assert isinstance(key, slice) and isinstance(key.start, int) and isinstance(key.stop, int) and key.step is None

        if self._reversed:
            cnt = self.count()
            start, stop = key.start, key.stop
            start = cnt - start
            stop = cnt - stop
            pks = reversed(redis.lrange(self._key, stop, start - 1))
        else:
            pks = redis.lrange(self._key, key.start, key.stop - 1)

        return get_cached_objects(pks, model=FlatComment, missing=SKIP)

    def last_comment(self):
        try:
            return self[0]
        except IndexError:
            return None

    def add_comment(self, comment):
        redis.lpush(self._key, comment.id)

    def remove_comment(self, comment):
        redis.lrem(self._key, 0, comment.id)

class CommentManager(models.Manager):
    def post_comment(self, comment, request):
        """
        Post comment, fire of all the signals connected to that event and see
        if any receiver shut the posting down.

        Return error boolean and reason for error, if any.
        """
        responses = comment_will_be_posted.send(self.model, comment=comment, request=request)
        for (receiver, response) in responses:
            if response == False:
                return False, "comment_will_be_posted receiver %r killed the comment" % receiver.__name__
        comment.save(force_insert=True)
        # add comment to redis
        CommentList(comment.content_type, comment.object_id).add_comment(comment)
        responses = comment_was_posted.send(self.model, comment=comment, request=request)
        return True, None

    def moderate_comment(self, comment, user):
        """
        Mark some comment as moderated and fire a signal to make other apps aware of this
        """
        if not comment.is_public:
            return
        comment.is_public = False
        self.filter(pk=comment.pk).update(is_public=False)
        # remove comment from redis
        CommentList(comment.content_type, comment.object_id).remove_comment(comment)
        comment_was_moderated.send(self.model, comment=comment, user=user)


class FlatComment(models.Model):
    site = SiteForeignKey(default=Site.objects.get_current)

    content_type = ContentTypeForeignKey()
    object_id = models.IntegerField()
    content_object = CachedGenericForeignKey('content_type', 'object_id')

    content = models.TextField()

    submit_date = models.DateTimeField(default=None)
    user = CachedForeignKey(User)
    is_public = models.BooleanField(default=True)

    app_data = AppDataField()

    objects = CommentManager()

    def delete(self):
        CommentList(self.content_type, self.object_id).remove_comment(self)
        if self.is_public:
            comment_was_moderated.send(self.__class__, comment=self, user=None)
        super(FlatComment, self).delete()

    def save(self, **kwargs):
        if self.submit_date is None:
            self.submit_date = timezone.now()
        super(FlatComment, self).save(**kwargs)
