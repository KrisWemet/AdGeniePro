import random

class OptimizerAgent:
    """
    Analyzes or optimizes ad copy/campaign performance.
    Currently simulates performance with a random score.
    """

    def optimize(self, ad_copy):
        performance_score = random.randint(1, 100)
        if performance_score > 50:
            return f"Ad performing well with score: {performance_score}"
        else:
            return f"Ad underperforming with score: {performance_score}. Consider revising."


if __name__ == "__main__":
    agent = OptimizerAgent()
    test_copy = "Try our new gadget to transform your daily routine!"
    print(agent.optimize(test_copy))
