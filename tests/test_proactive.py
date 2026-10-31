import unittest
from assistant.proactive import SuggestionPolicy


class ProactiveTests(unittest.TestCase):
    def sample(self,policy,now,**kwargs):
        args=dict(now=now,hour=12,idle_seconds=0,free_bytes=10*1024**3)
        args.update(kwargs)
        return policy.poll(**args)

    def test_break_cooldown_and_real_inactivity_reset(self):
        policy=SuggestionPolicy()
        self.assertIsNone(self.sample(policy,0))
        self.assertIn('перерыв',self.sample(policy,3000))
        self.assertIsNone(self.sample(policy,3010))
        self.sample(policy,4000,idle_seconds=600)
        self.assertIsNone(self.sample(policy,6000))
        self.assertIn('перерыв',self.sample(policy,9000))

    def test_quiet_hours_fullscreen_and_task_suppress_suggestions(self):
        for flags in ({'hour':23},{'fullscreen':True},{'busy':True},{'idle_seconds':400}):
            policy=SuggestionPolicy()
            self.assertIsNone(self.sample(policy,0,free_bytes=1,**flags))
            self.assertIn('2 ГБ',self.sample(policy,10,free_bytes=1))
            self.assertIsNone(self.sample(policy,20,free_bytes=1))
